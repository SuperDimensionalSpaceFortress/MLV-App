// LOOK-LABEL-PLAYBACK-INFO-1: the playback-info lines under the fullscreen look label. Owner, 2026-11-06 (PR 459):
// "We need to show other useful info as well. CPU vs GPU. Scale (Full, Half, Quarter)." Owner, 2026-11-16: "show other
// useful info such as fps, resolution, etc". compose() is pure (PlaybackInfoLabelPolicy.h); MainWindow, the surfaces and
// the receipt are not linked into console_tests, so their wiring is pinned as source text.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackFpsMeterPolicy.h"
#include "../../platform/qt/PlaybackInfoLabelPolicy.h"
#include "../../platform/qt/PlaybackScaleLabel.h"

#include <QChar>
#include <QDir>
#include <QFile>
#include <QList>
#include <QString>
#include <QTextStream>
#include <QUuid>

#include <string>

#define ASSERT_QSTR_EQ(EXPECTED, ACTUAL) \
    ASSERT_EQ(std::string(QString(EXPECTED).toStdString()), std::string(QString(ACTUAL).toStdString()))

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

QString dot()
{
    return QStringLiteral(" ") + QChar(0x00B7) + QStringLiteral(" ");
}

// The owner-shape facts: CUDA display, GPU Preview, full res, AMaZE on GPU, a 24 fps Dual-ISO clip at 1920x1080.
playback_info_label::Facts ownerFacts()
{
    playback_info_label::Facts f;
    f.gpuDisplay = true;
    f.processingBadge = QStringLiteral("GPU Preview");
    f.requestedScale = 1;
    f.presentedScale = 1;
    f.presentedValid = true;
    f.debayer = QStringLiteral("AMaZE");
    f.debayerOnGpu = true;
    f.playing = false;
    f.achievedFps = 0.0;
    f.clipFps = 24.0;
    f.playbackRate = 24.0;
    f.dropFrame = true;
    f.dualIsoClip = true;
    f.dualIsoOn = true;
    f.width = 1920;
    f.height = 1080;
    return f;
}

playback_info_label::Toggles togglesFromMask(int mask)
{
    playback_info_label::Toggles t;
    t.pipeline = (mask & 1) != 0;
    t.scale = (mask & 2) != 0;
    t.debayer = (mask & 4) != 0;
    t.fps = (mask & 8) != 0;
    t.dualIso = (mask & 16) != 0;
    t.resolution = (mask & 32) != 0;
    return t;
}

QString functionBody(const QString &source, const QString &signature)
{
    const int start = source.indexOf(signature);
    ASSERT_TRUE(start >= 0);
    if ( start < 0 ) return QString();
    const int end = source.indexOf(QStringLiteral("\n}\n"), start);
    return source.mid(start, end > start ? end - start : -1);
}

} // namespace

TEST(PlaybackScaleLabel, FractionWord)
{
    ASSERT_QSTR_EQ(QStringLiteral("Full"), PlaybackScaleLabel::fractionWord(1));
    ASSERT_QSTR_EQ(QStringLiteral("Half"), PlaybackScaleLabel::fractionWord(2));
    ASSERT_QSTR_EQ(QStringLiteral("Quarter"), PlaybackScaleLabel::fractionWord(4));
    ASSERT_QSTR_EQ(QStringLiteral("Eighth"), PlaybackScaleLabel::fractionWord(8));
    ASSERT_QSTR_EQ(QStringLiteral("1/3"), PlaybackScaleLabel::fractionWord(3));
    ASSERT_QSTR_EQ(QStringLiteral("1/16"), PlaybackScaleLabel::fractionWord(16));
}

TEST(PlaybackInfoLabel, OwnerShapeStopped)
{
    const QString text = playback_info_label::compose(ownerFacts(), playback_info_label::Toggles(), true);
    ASSERT_QSTR_EQ(QStringLiteral("GPU display GPU Preview") + dot() + QStringLiteral("Full") + dot()
                       + QStringLiteral("AMaZE (GPU)") + QStringLiteral("\n")
                       + QStringLiteral("24 fps") + dot() + QStringLiteral("DF on") + dot()
                       + QStringLiteral("Dual-ISO on") + dot() + QStringLiteral("1920x1080"),
                   text);
}

TEST(PlaybackInfoLabel, PlayingShowsAchievedOverClip)
{
    playback_info_label::Facts f = ownerFacts();
    f.playing = true;
    f.achievedFps = 23.96;
    const QString text = playback_info_label::compose(f, playback_info_label::Toggles(), true);
    ASSERT_TRUE(text.contains(QStringLiteral("\n24.0/24 fps") + dot() + QStringLiteral("DF on")));
    // Not yet measured (the first ticks of a play): "--", never "0.0".
    f.achievedFps = 0.0;
    ASSERT_TRUE(playback_info_label::compose(f, playback_info_label::Toggles(), true)
                    .contains(QStringLiteral("--/24 fps")));
    // A fractional clip rate prints as the status bar prints it.
    f.achievedFps = 29.0;
    f.clipFps = 29.97;
    f.playbackRate = 29.97;
    ASSERT_TRUE(playback_info_label::compose(f, playback_info_label::Toggles(), true)
                    .contains(QStringLiteral("29.0/29.97 fps")));
}

TEST(PlaybackInfoLabel, ConformSuffixOnlyBelowClipRate)
{
    playback_info_label::Facts f = ownerFacts();
    f.playing = true;
    f.achievedFps = 24.0;
    f.clipFps = 60.0;
    f.playbackRate = 24.0;
    ASSERT_TRUE(playback_info_label::compose(f, playback_info_label::Toggles(), true)
                    .contains(QStringLiteral("24.0/60 fps (60->24)")));
    // Within 0.005 of the clip rate: no suffix.
    f.playbackRate = 59.996;
    ASSERT_FALSE(playback_info_label::compose(f, playback_info_label::Toggles(), true).contains(QStringLiteral("->")));
    // Stopped: the clip rate only, no suffix.
    f.playing = false;
    f.playbackRate = 24.0;
    const QString stopped = playback_info_label::compose(f, playback_info_label::Toggles(), true);
    ASSERT_TRUE(stopped.contains(QStringLiteral("\n60 fps") + dot()));
    ASSERT_FALSE(stopped.contains(QStringLiteral("(60->24)")));
}

TEST(PlaybackInfoLabel, ScaleWordsAndRequestedToPresented)
{
    playback_info_label::Facts f = ownerFacts();
    const int scales[] = {1, 2, 4, 8, 3};
    const char *words[] = {"Full", "Half", "Quarter", "Eighth", "1/3"};
    for ( int i = 0; i < 5; ++i )
    {
        f.requestedScale = scales[i];
        f.presentedScale = scales[i];
        ASSERT_TRUE(playback_info_label::compose(f, playback_info_label::Toggles(), true)
                        .contains(dot() + QString::fromLatin1(words[i]) + dot()));
    }
    f.requestedScale = 1;
    f.presentedScale = 2;
    f.presentedValid = true;
    ASSERT_TRUE(playback_info_label::compose(f, playback_info_label::Toggles(), true)
                    .contains(dot() + QStringLiteral("Full->Half") + dot()));
    // A presented scale of another request is not shown.
    f.presentedValid = false;
    ASSERT_TRUE(playback_info_label::compose(f, playback_info_label::Toggles(), true)
                    .contains(dot() + QStringLiteral("Full") + dot()));
    // Unknown requested scale: the field is omitted.
    f.requestedScale = 0;
    ASSERT_QSTR_EQ(QStringLiteral("GPU display GPU Preview") + dot() + QStringLiteral("AMaZE (GPU)"),
                   playback_info_label::compose(f, playback_info_label::Toggles(), true).section(QLatin1Char('\n'), 0, 0));
}

TEST(PlaybackInfoLabel, PipelineAndDebayerForms)
{
    playback_info_label::Facts f = ownerFacts();
    f.gpuDisplay = false;
    f.processingBadge = QStringLiteral("CPU");
    f.debayer = QStringLiteral("Bilinear");
    f.debayerOnGpu = false;
    const QString lineA = playback_info_label::compose(f, playback_info_label::Toggles(), true).section(QLatin1Char('\n'), 0, 0);
    ASSERT_QSTR_EQ(QStringLiteral("CPU display CPU") + dot() + QStringLiteral("Full") + dot() + QStringLiteral("Bilinear"), lineA);
    f.processingBadge.clear();
    f.debayer.clear();
    ASSERT_QSTR_EQ(QStringLiteral("CPU display") + dot() + QStringLiteral("Full"),
                   playback_info_label::compose(f, playback_info_label::Toggles(), true).section(QLatin1Char('\n'), 0, 0));
}

TEST(PlaybackInfoLabel, DualIsoOmittedForNonDualIsoClip)
{
    playback_info_label::Facts f = ownerFacts();
    f.dualIsoClip = false;
    f.dualIsoOn = false;
    const QString text = playback_info_label::compose(f, playback_info_label::Toggles(), true);
    ASSERT_FALSE(text.contains(QStringLiteral("Dual-ISO")));
    ASSERT_TRUE(text.endsWith(QStringLiteral("24 fps") + dot() + QStringLiteral("DF on") + dot() + QStringLiteral("1920x1080")));
    f.dualIsoClip = true;
    ASSERT_TRUE(playback_info_label::compose(f, playback_info_label::Toggles(), true).contains(QStringLiteral("Dual-ISO off")));
}

TEST(PlaybackInfoLabel, StatusTwinDropsRateKeepsDropFrame)
{
    playback_info_label::Facts f = ownerFacts();
    f.playing = true;
    f.achievedFps = 23.9;
    f.dropFrame = false;
    const QString twin = playback_info_label::compose(f, playback_info_label::Toggles(), false);
    ASSERT_FALSE(twin.contains(QStringLiteral("fps")));
    ASSERT_TRUE(twin.contains(QStringLiteral("\nDF off") + dot() + QStringLiteral("Dual-ISO on")));
}

// Every toggle combination: each enabled known field appears, each disabled one does not, at most two lines,
// no empty line, and every-field-off is the empty string.
TEST(PlaybackInfoLabel, EveryToggleCombination)
{
    playback_info_label::Facts f = ownerFacts();
    for ( int playing = 0; playing < 2; ++playing )
    {
        f.playing = playing != 0;
        f.achievedFps = f.playing ? 23.5 : 0.0;
        for ( int mask = 0; mask < 64; ++mask )
        {
            const playback_info_label::Toggles t = togglesFromMask(mask);
            const QString text = playback_info_label::compose(f, t, true);
            ASSERT_EQ(t.pipeline, text.contains(QStringLiteral("GPU display")));
            ASSERT_EQ(t.scale, text.contains(QStringLiteral("Full")));
            ASSERT_EQ(t.debayer, text.contains(QStringLiteral("AMaZE (GPU)")));
            ASSERT_EQ(t.fps, text.contains(QStringLiteral(" fps")));
            ASSERT_EQ(t.fps, text.contains(QStringLiteral("DF on")));
            ASSERT_EQ(t.dualIso, text.contains(QStringLiteral("Dual-ISO on")));
            ASSERT_EQ(t.resolution, text.contains(QStringLiteral("1920x1080")));
            ASSERT_EQ(f.playing && t.fps, text.contains(QStringLiteral("23.5/24 fps")));
            const QStringList lines = text.split(QLatin1Char('\n'));
            ASSERT_TRUE(lines.size() <= 2);
            const bool anyA = t.pipeline || t.scale || t.debayer;
            const bool anyB = t.fps || t.dualIso || t.resolution;
            ASSERT_EQ((anyA ? 1 : 0) + (anyB ? 1 : 0), text.isEmpty() ? 0 : static_cast<int>(lines.size()));
            for ( const QString &line : lines )
            {
                if ( text.isEmpty() ) break;
                ASSERT_FALSE(line.isEmpty());
                ASSERT_FALSE(line.startsWith(dot()));
                ASSERT_FALSE(line.endsWith(dot()));
            }
            ASSERT_EQ(mask == 0, text.isEmpty());
            const QString twin = playback_info_label::compose(f, t, false);
            ASSERT_FALSE(twin.contains(QStringLiteral(" fps")));
        }
    }
}

TEST(PlaybackInfoLabel, NothingKnownIsEmpty)
{
    const playback_info_label::Facts none;   // no clip: no scale, debayer, rate or size
    playback_info_label::Toggles t;
    t.pipeline = false;
    t.fps = false;
    ASSERT_TRUE(playback_info_label::compose(none, t, true).isEmpty());
}

TEST(PlaybackInfoLabel, LogFormIsOneQuotedSafeLine)
{
    playback_info_label::Facts f = ownerFacts();
    f.debayer = QStringLiteral("He said \"AMaZE\"");
    const QString log = playback_info_label::logForm(playback_info_label::compose(f, playback_info_label::Toggles(), true));
    ASSERT_FALSE(log.contains(QLatin1Char('\n')));
    ASSERT_FALSE(log.contains(QLatin1Char('"')));
    ASSERT_TRUE(log.contains(QStringLiteral(" / 24 fps")));
    ASSERT_TRUE(log.contains(QStringLiteral("'AMaZE'")));
    ASSERT_QSTR_EQ(QString(), playback_info_label::logForm(QString()));
}

TEST(PlaybackInfoLabel, FieldsCsv)
{
    ASSERT_QSTR_EQ(QStringLiteral("pipeline,scale,debayer,fps,dual_iso,resolution"),
                   playback_info_label::fieldsCsv(playback_info_label::Toggles()));
    ASSERT_QSTR_EQ(QStringLiteral("none"), playback_info_label::fieldsCsv(togglesFromMask(0)));
    ASSERT_QSTR_EQ(QStringLiteral("scale,fps"), playback_info_label::fieldsCsv(togglesFromMask(2 | 8)));
}

TEST(PlaybackInfoLabel, SourceStaysAscii)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/PlaybackInfoLabelPolicy.h"));
    for ( const QChar c : header ) ASSERT_TRUE(c.unicode() < 0x80);
}

// The MainWindow wiring (not linked here): the fps hook is gated on the shown overlay and the fps toggle, the overlay
// text appends the info lines under the look line, and the receipt appends its fields after wait_ms=.
TEST(PlaybackInfoLabel, MainWindowWiring)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString push = functionBody(source, QStringLiteral("void MainWindow::pushLookLabelOverlay("));
    ASSERT_TRUE(push.contains(QStringLiteral("m_lookLabelText + QStringLiteral( \"\\n\" ) + m_playbackInfoText")));
    ASSERT_TRUE(push.contains(QStringLiteral("GpuDisplayWindow::setLookLabelIfActive( m_lookLabelOverlayText, repaintNow )")));
    const QString update = functionBody(source, QStringLiteral("void MainWindow::updateLookLabel("));
    ASSERT_TRUE(update.contains(QStringLiteral("pushLookLabelOverlay();")));
    ASSERT_TRUE(source.contains(QStringLiteral("m_lookLabelFullscreenShown && m_playbackInfoToggles.fps")));
    const int waitAt = source.indexOf(QStringLiteral("wait_ms=%5"));
    const int infoAt = source.indexOf(QStringLiteral("info_shown=\\\"%6\\\" info_expected=\\\"%7\\\" info_fields=%8"));
    ASSERT_TRUE(waitAt >= 0);
    ASSERT_TRUE(infoAt > waitAt);
    ASSERT_TRUE(source.contains(QStringLiteral("gui_smoke.look_label_playing shown=")));
    // Never playbackDebayerLabel() (it returns "receipt"): the chosen debayer's menu text.
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts("));
    // INFO-2 r1 (pass 21 A1): the facts read it through playbackInfoDebayerName (the relabel key's reader), which falls back to it
    const QString reader = functionBody(source, QStringLiteral("QString MainWindow::playbackInfoDebayerName( void ) const"));
    ASSERT_TRUE(facts.contains(QStringLiteral("playbackInfoDebayerName()")));
    ASSERT_TRUE(reader.contains(QStringLiteral("m_pChosenDebayer->text()")));
    ASSERT_FALSE(facts.contains(QStringLiteral("playbackDebayerLabel(")));
    ASSERT_FALSE(reader.contains(QStringLiteral("playbackDebayerLabel(")));
}

// r1 adversary A1 (06898800): the toolbar indicator runs per frame in Auto mode (its cache holds the per-frame decision stats),
// so a hook there composed the live fps EMA ~25 times a second. The rate shown is a snapshot taken ONLY at the fps meter's
// 250 ms gate (and its reset), so no hook can change the fps text faster than 4 Hz, and the indicator relabels only when an
// info input changed (int compares, before its cache-hit return), never unconditionally.
TEST(PlaybackInfoLabel, R1A1FpsShownOnlyFromTheGateSnapshot)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts("));
    ASSERT_TRUE(facts.contains(QStringLiteral("facts.achievedFps = m_playbackInfoFpsShown;")));
    ASSERT_FALSE(facts.contains(QStringLiteral("m_playbackFpsEmaFrameMs")));
    // Written at the gate (beside m_pFpsStatus), at the reset, and (r7 pass 11 A1) zeroed by the Play toggle, nowhere else.
    ASSERT_EQ(3, static_cast<int>(source.count(QStringLiteral("m_playbackInfoFpsShown = "))));
    ASSERT_TRUE(source.contains(QStringLiteral("m_playbackInfoFpsShown = smoothedFps;")));
    ASSERT_TRUE(source.contains(QStringLiteral("m_playbackInfoFpsShown = 0.0;")));
    const QString indicator = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackQualityIndicator("));
    const int keyAt = indicator.indexOf(QStringLiteral("refreshPlaybackInfoOnInputChange( gpuPlaybackPipelineStatus, scopePerformanceCaveat );"));
    const int cacheHitAt = indicator.indexOf(QStringLiteral("if( m_playbackQualityIndicatorCacheValid"));
    ASSERT_TRUE(keyAt >= 0);
    ASSERT_TRUE(keyAt < cacheHitAt);
    ASSERT_FALSE(indicator.contains(QStringLiteral("updatePlaybackInfoLabel();")));
}

// r1 adversary A3 (06898800): a CPU AMaZE fallback (CA correction) clears the presented GPU-debayer flag without changing the
// aggregate pipeline status, so the indicator cache hit kept "AMaZE (GPU)". Every presented frame compares the info inputs
// (the debayer flags, the presented scale and its request) and relabels only on a change.
TEST(PlaybackInfoLabel, R1A3PresentedFrameRelabelsOnAnInfoInputChange)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString record = functionBody(source, QStringLiteral("void MainWindow::recordPresentedFrame("));
    ASSERT_TRUE(record.contains(QStringLiteral("refreshPlaybackInfoOnInputChange( gpuPlaybackPipelineStatus, m_playbackInfoInputs.scopeCaveat );")));
    const QString key = functionBody(source, QStringLiteral("void MainWindow::refreshPlaybackInfoOnInputChange("));
    ASSERT_TRUE(key.contains(QStringLiteral("m_lastPresentedFrameUsedGpuAmazeDebayer")));
    ASSERT_TRUE(key.contains(QStringLiteral("m_lastPresentedFrameUsedGpuBilinearDebayer")));
    // r12 pass-13 A1: the presented scale through the one accessor, which still starts from the render's own report
    ASSERT_TRUE(key.contains(QStringLiteral("inputs.presentedScale = presentedPlaybackScaleShown();")));
    ASSERT_TRUE(functionBody(source, QStringLiteral("int MainWindow::presentedPlaybackScaleShown("))
                    .contains(QStringLiteral("m_lastPresentedPlaybackScaleFactorActive")));
    ASSERT_TRUE(key.contains(QStringLiteral("m_lastPresentedRequestContext.playbackScaleFactor")));
    ASSERT_TRUE(key.contains(QStringLiteral("playbackScaleFactorPolicyDecision()")));
    ASSERT_TRUE(key.contains(QStringLiteral("updatePlaybackInfoLabel();")));
}

// r1 pass-3 A1 (a472a49f): while playing the GPU window only stores a changed label (the next frame paints it), but a
// withdrawal (the label hidden, or the measured rate reset because frames stopped) must repaint at once: no frame may come.
TEST(PlaybackInfoLabel, R1P3A1WithdrawalRepaintsAtOnce)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString push = functionBody(source, QStringLiteral("void MainWindow::pushLookLabelOverlay("));
    // r2 pass-2 A2: the rate withdrawal is the reset's flag (R2P2A2OnlyAWithdrawalRepaintsWhilePlaying). r3 pass-2 A1: and a stall
    // (R3P2A1StalledFramesPaintTheStoredLabel).
    ASSERT_TRUE(push.contains(QStringLiteral(
        "const bool repaintNow = !ui->actionPlay->isChecked() || m_lookLabelOverlayText.isEmpty() || m_playbackInfoRateWithdrawn"
        " || m_playbackFramesStalled;")));
    ASSERT_TRUE(push.contains(QStringLiteral("GpuDisplayWindow::setLookLabelIfActive( m_lookLabelOverlayText, repaintNow )")));
}

// r1 pass-3 A2 (a472a49f): a presented-frame input that flips every frame (GPU AMaZE on/off) relabelled per frame. While
// playing, an input change only marks the info dirty; the fps meter's 250 ms gate relabels, so changes stay at most 4 Hz.
TEST(PlaybackInfoLabel, R1P3A2PlayingInputChangeWaitsForTheGate)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString key = functionBody(source, QStringLiteral("void MainWindow::refreshPlaybackInfoOnInputChange("));
    const int dirtyAt = key.indexOf(QStringLiteral("if( ui->actionPlay->isChecked() ) { m_playbackInfoInputsDirty = true; return; }"));
    ASSERT_TRUE(dirtyAt >= 0);
    ASSERT_TRUE(dirtyAt < key.indexOf(QStringLiteral("updatePlaybackInfoLabel();")));
    // r2 A1: the relabel that follows is the monotonic gate's (R2A1DeferredRelabelHasItsOwnMonotonicGate).
    ASSERT_TRUE(source.contains(QStringLiteral("void MainWindow::flushPlaybackInfoIfDue(")));
    const QString update = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackInfoLabel("));
    ASSERT_TRUE(update.contains(QStringLiteral("m_playbackInfoInputsDirty = false;")));
}

// r2 adversary A1 (deeae9f4): a deferred info change (scale, pipeline, debayer) waited for the fps meter's gate, which reads the
// wall-clock QTime (after 23:59:59.900 it never reads 250 ms later until the clock catches up) and needs a measured rate (a 1 fps
// conform resets the meter first). The deferred relabel has its own MONOTONIC 250 ms gate, checked on every timer tick; the fps
// gate only marks the rate dirty, so every playing relabel still goes through the one <= 4 Hz gate.
TEST(PlaybackInfoLabel, R2A1DeferredRelabelHasItsOwnMonotonicGate)
{
    ASSERT_FALSE(playback_info_label::deferredRelabelDue(false, false, 0));
    ASSERT_FALSE(playback_info_label::deferredRelabelDue(false, true, 86400000));
    ASSERT_TRUE(playback_info_label::deferredRelabelDue(true, false, 0));
    ASSERT_FALSE(playback_info_label::deferredRelabelDue(true, true, 0));
    ASSERT_FALSE(playback_info_label::deferredRelabelDue(true, true, 249));
    ASSERT_TRUE(playback_info_label::deferredRelabelDue(true, true, 250));
    ASSERT_TRUE(playback_info_label::deferredRelabelDue(true, true, 86400000));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString flush = functionBody(source, QStringLiteral("void MainWindow::flushPlaybackInfoIfDue("));
    ASSERT_TRUE(flush.contains(QStringLiteral("playback_info_label::deferredRelabelDue(")));
    // r6: the call's whole bool argument list (dirty, clockValid), so an inverted argument fails here.
    ASSERT_TRUE(flush.contains(QStringLiteral(
        "if( playback_info_label::deferredRelabelDue( m_playbackInfoInputsDirty, clockValid,")));
    ASSERT_TRUE(flush.contains(QStringLiteral("clockValid ? m_playbackInfoRelabelClock.elapsed() : 0 ) )")));
    ASSERT_TRUE(flush.contains(QStringLiteral("m_playbackInfoRelabelClock.elapsed()")));
    ASSERT_FALSE(flush.contains(QStringLiteral("QTime")));
    ASSERT_TRUE(flush.contains(QStringLiteral("updatePlaybackInfoLabel();")));
    // Every tick, right after the Look Assist pump's return and before any other early return of the tick.
    const QString tick = functionBody(source, QStringLiteral("void MainWindow::timerFrameEvent("));
    const int pumpAt = tick.indexOf(QStringLiteral("if( m_lookAssistPump.sessionOpen() ) return;"));
    const int flushAt = tick.indexOf(QStringLiteral("flushPlaybackInfoIfDue();"));
    ASSERT_TRUE(pumpAt >= 0);
    ASSERT_TRUE(flushAt > pumpAt);
    ASSERT_TRUE(flushAt < tick.indexOf(QStringLiteral("if( m_frameStillDrawing )")));
    // The fps gate marks the rate dirty and asks the same gate; it never relabels on the wall clock by itself.
    ASSERT_FALSE(source.contains(QStringLiteral("m_playbackInfoInputsDirty || ( m_lookLabelFullscreenShown")));
    ASSERT_TRUE(tick.contains(QStringLiteral(
        "if( m_lookLabelFullscreenShown && m_playbackInfoToggles.fps ) m_playbackInfoInputsDirty = true;")));
    ASSERT_TRUE(tick.indexOf(QStringLiteral("flushPlaybackInfoIfDue();"), flushAt + 1) > flushAt);
    // A relabel restarts the gate's clock.
    const QString update = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackInfoLabel("));
    ASSERT_TRUE(update.contains(QStringLiteral("m_playbackInfoRelabelClock.start();")));
}

// r2 pass-2 A1 (1fbfa178): a direct relabel caller (a menu toggle flipped six times in 250 ms, the fullscreen switch, a clip
// hook) skipped the gate while playing. The gate is now INSIDE updatePlaybackInfoLabel: while playing, a call within 250 ms of
// the last relabel only marks the info dirty (the per-tick flush relabels), so every playing relabel stays at most 4 Hz.
TEST(PlaybackInfoLabel, R2P2A1EveryPlayingRelabelPassesTheOneGate)
{
    ASSERT_FALSE(playback_info_label::playingRelabelDeferred(false, true, 0));
    ASSERT_FALSE(playback_info_label::playingRelabelDeferred(true, false, 0));
    ASSERT_TRUE(playback_info_label::playingRelabelDeferred(true, true, 0));
    ASSERT_TRUE(playback_info_label::playingRelabelDeferred(true, true, 249));
    ASSERT_FALSE(playback_info_label::playingRelabelDeferred(true, true, 250));
    // A due flush is never deferred by the same gate.
    for ( const qint64 ms : {qint64(0), qint64(249), qint64(250), qint64(86400000)} )
        ASSERT_FALSE(playback_info_label::deferredRelabelDue(true, true, ms)
                     && playback_info_label::playingRelabelDeferred(true, true, ms));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString update = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackInfoLabel("));
    const int gateAt = update.indexOf(QStringLiteral("playback_info_label::playingRelabelDeferred("));
    const int deferAt = update.indexOf(QStringLiteral("m_playbackInfoInputsDirty = true;"));
    const int startAt = update.indexOf(QStringLiteral("m_playbackInfoRelabelClock.start();"));
    ASSERT_TRUE(gateAt >= 0);
    ASSERT_TRUE(deferAt > gateAt);
    ASSERT_TRUE(startAt > deferAt);
    ASSERT_TRUE(update.indexOf(QStringLiteral("ui->actionPlay->isChecked()")) < startAt);
    // r6: the call's whole bool argument list (playing, clockValid), so an inverted argument fails here.
    ASSERT_TRUE(update.contains(QStringLiteral(
        "if( !forceWithdrawal && playback_info_label::playingRelabelDeferred( ui->actionPlay->isChecked(), relabelClockValid,")));
    ASSERT_TRUE(update.contains(QStringLiteral("relabelClockValid ? m_playbackInfoRelabelClock.elapsed() : 0 ) )")));
    // r6: compose's includeFps. The overlay carries the rate (true); the status-bar twin sits beside the fps meter (false).
    ASSERT_TRUE(update.contains(QStringLiteral(
        "? playback_info_label::compose( facts, m_playbackInfoToggles, true ) : QString();")));
    ASSERT_TRUE(update.contains(QStringLiteral(
        "const QString twin = clipLoaded ? playback_info_label::compose( facts, m_playbackInfoToggles, false ) : QString();")));
}

// r2 pass-2 A2 (1fbfa178): starting Play from a paused frame (no rate measured yet) made repaintNow true, so the GPU window
// repainted the retained paused frame: an extra paint and swap. Only a WITHDRAWAL repaints at once while playing: the label
// hidden, or a measured rate reset because frames stopped (the reset sets the flag; the next push consumes it).
TEST(PlaybackInfoLabel, R2P2A2OnlyAWithdrawalRepaintsWhilePlaying)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString push = functionBody(source, QStringLiteral("void MainWindow::pushLookLabelOverlay("));
    ASSERT_FALSE(push.contains(QStringLiteral("m_playbackInfoFpsShown <= 0.0")));
    // r3 pass-2 A1: a stall (no draw for > 500 ms after one) is no Play-from-paused start: the flag needs a draw first.
    ASSERT_TRUE(push.contains(QStringLiteral(
        "const bool repaintNow = !ui->actionPlay->isChecked() || m_lookLabelOverlayText.isEmpty() || m_playbackInfoRateWithdrawn"
        " || m_playbackFramesStalled;")));
    // The push never clears it (a look-line push while the info relabel waits must not eat the withdrawal); the info relabel
    // clears it after its own push, or on each early return past the gate (nothing to withdraw).
    ASSERT_FALSE(push.contains(QStringLiteral("m_playbackInfoRateWithdrawn = false;")));
    const QString update = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackInfoLabel("));
    ASSERT_EQ(3, static_cast<int>(update.count(QStringLiteral("m_playbackInfoRateWithdrawn = false;"))));
    ASSERT_TRUE(update.lastIndexOf(QStringLiteral("m_playbackInfoRateWithdrawn = false;"))
                > update.indexOf(QStringLiteral("pushLookLabelOverlay();")));
    ASSERT_TRUE(update.indexOf(QStringLiteral("m_playbackInfoRateWithdrawn = false;"))
                > update.indexOf(QStringLiteral("m_playbackInfoInputsDirty = true;")));
    const QString tick = functionBody(source, QStringLiteral("void MainWindow::timerFrameEvent("));
    const int setAt = tick.indexOf(QStringLiteral("m_playbackInfoRateWithdrawn = true;"));
    ASSERT_TRUE(setAt >= 0);
    ASSERT_TRUE(tick.indexOf(QStringLiteral("fpsWasMeasured")) < setAt);
    ASSERT_EQ(1, static_cast<int>(source.count(QStringLiteral("m_playbackInfoRateWithdrawn = true;"))));
}

// r2 pass-3 A1 (c65933cb): updateLookLabel composed the info lines itself and pushed them outside the 250 ms relabel gate, so
// a field toggle then the fullscreen label off/on relabelled info 80 ms apart. updateLookLabel never composes info: it pushes the
// look line with the CACHED info (a hide is a withdrawal: the overlay is empty and repaints at once), and on a route change asks
// updatePlaybackInfoLabel, which is gated while playing. The info text changes only inside updatePlaybackInfoLabel.
TEST(PlaybackInfoLabel, R3A1UpdateLookLabelNeverComposesInfo)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString update = functionBody(source, QStringLiteral("void MainWindow::updateLookLabel("));
    ASSERT_FALSE(update.isEmpty());
    ASSERT_FALSE(update.contains(QStringLiteral("playback_info_label::compose(")));
    ASSERT_FALSE(update.contains(QStringLiteral("playbackInfoFacts(")));
    ASSERT_FALSE(update.contains(QStringLiteral("m_playbackInfoText =")));
    // The one writer of the info text is the gated relabel.
    ASSERT_EQ(1, static_cast<int>(source.count(QStringLiteral("m_playbackInfoText ="))));
    const QString relabel = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackInfoLabel("));
    ASSERT_TRUE(relabel.contains(QStringLiteral("m_playbackInfoText = overlay;")));
    // A route change (the corner label shown or hidden) asks the gated relabel, after the look line's own push.
    const int routeAt = update.indexOf(QStringLiteral("const bool routeChanged = fullscreenShown != m_lookLabelFullscreenShown;"));
    const int pushAt = update.indexOf(QStringLiteral("pushLookLabelOverlay();"));
    const int askAt = update.indexOf(QStringLiteral("if( routeChanged ) updatePlaybackInfoLabel();"));
    ASSERT_TRUE(routeAt >= 0);
    ASSERT_TRUE(pushAt > routeAt);
    ASSERT_TRUE(askAt > pushAt);
    // Never the forced (withdrawal) relabel: that bypass belongs to the fps meter's reset alone (R3A2...).
    ASSERT_FALSE(update.contains(QStringLiteral("updatePlaybackInfoLabel( true )")));
}

// r2 pass-3 A2 (c65933cb): a reset deferred by the playing gate kept m_playbackInfoRateWithdrawn set; a new measured rate did not
// cancel it, so the later push of an ordinary "25.0/25 fps" relabel repainted the retained GPU frame (an extra playing paint), and
// the withdrawal itself waited for the gate while stalled. (a) A measured rate is not a withdrawal: the fps gate clears the flag
// when it takes the new snapshot. (b) The withdrawal relabel is forced past the playing gate (it needs a > 500 ms draw gap first,
// so it runs at most 2 Hz on its own); every other caller stays gated.
TEST(PlaybackInfoLabel, R3A2WithdrawalIsPromptAndAMeasuredRateCancelsIt)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString tick = functionBody(source, QStringLiteral("void MainWindow::timerFrameEvent("));
    // (a) right after the gate's snapshot, before the gate's flush.
    const int snapAt = tick.indexOf(QStringLiteral("m_playbackInfoFpsShown = smoothedFps;"));
    const int cancelAt = tick.indexOf(QStringLiteral("m_playbackInfoRateWithdrawn = false;"), snapAt);
    const int gateFlushAt = tick.indexOf(QStringLiteral("flushPlaybackInfoIfDue();"), snapAt);
    ASSERT_TRUE(snapAt >= 0);
    ASSERT_TRUE(cancelAt > snapAt);
    ASSERT_TRUE(gateFlushAt > cancelAt);
    // (b) the reset forces its relabel; it is the only forced caller.
    const int setAt = tick.indexOf(QStringLiteral("m_playbackInfoRateWithdrawn = true;"));
    const int forcedAt = tick.indexOf(QStringLiteral("updatePlaybackInfoLabel( true );"));
    ASSERT_TRUE(setAt >= 0);
    ASSERT_TRUE(forcedAt > setAt);
    ASSERT_TRUE(forcedAt < snapAt);
    ASSERT_EQ(1, static_cast<int>(source.count(QStringLiteral("updatePlaybackInfoLabel( true )"))));
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("void updatePlaybackInfoLabel( bool forceWithdrawal = false );")));
    const QString relabel = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackInfoLabel("));
    ASSERT_TRUE(relabel.contains(QStringLiteral("if( !forceWithdrawal && playback_info_label::playingRelabelDeferred(")));
    // The gated flush never forces.
    const QString flush = functionBody(source, QStringLiteral("void MainWindow::flushPlaybackInfoIfDue("));
    ASSERT_FALSE(flush.contains(QStringLiteral("updatePlaybackInfoLabel( true )")));
}

// r3 pass-1 A1 (9c9941cd): the GPU texture route clamps an x8 request to 1 and presents Full, but the presented request's
// scale (the EFFECTIVE 1) was compared with the requested (policy) 8, so presentedValid was false and both info surfaces and the
// toolbar indicator read "Eighth" instead of "Eighth->Full". The presented request carries its POLICY scale too, and the match
// is on that (playback_info_label::presentedScaleBelongsToRequest), for the label, its relabel key and the toolbar sibling.
TEST(PlaybackInfoLabel, R3P1A1ClampedRequestShowsThePresentedScale)
{
    // The clamp case: asked 8, the presented request was asked 8 too, presented Full.
    ASSERT_TRUE(playback_info_label::presentedScaleBelongsToRequest(true, 8, 8, 1));
    ASSERT_TRUE(playback_info_label::presentedScaleBelongsToRequest(true, 2, 2, 2));
    // Another request's frame, no presented frame, or an unknown active scale: not shown.
    ASSERT_FALSE(playback_info_label::presentedScaleBelongsToRequest(true, 4, 8, 1));
    ASSERT_FALSE(playback_info_label::presentedScaleBelongsToRequest(false, 8, 8, 1));
    ASSERT_FALSE(playback_info_label::presentedScaleBelongsToRequest(true, 8, 8, 3));
    ASSERT_FALSE(playback_info_label::presentedScaleBelongsToRequest(true, 0, 0, 1));
    playback_info_label::Facts f = ownerFacts();
    f.requestedScale = 8;
    f.presentedScale = 1;
    f.presentedValid = playback_info_label::presentedScaleBelongsToRequest(true, 8, 8, 1);
    ASSERT_TRUE(playback_info_label::compose(f, playback_info_label::Toggles(), true)
                    .contains(dot() + QStringLiteral("Eighth->Full") + dot()));

    const QString thread = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.h"));
    ASSERT_TRUE(thread.contains(QStringLiteral("int playbackScaleFactorPolicy = 0;")));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int effectiveAt = source.indexOf(QStringLiteral("requestContext.playbackScaleFactor = effectivePlaybackScaleFactorForRequest();"));
    const int policyAt = source.indexOf(QStringLiteral("requestContext.playbackScaleFactorPolicy = playbackScaleFactorPolicyDecision();"));
    ASSERT_TRUE(effectiveAt >= 0);
    ASSERT_TRUE(policyAt > effectiveAt);
    ASSERT_EQ(1, static_cast<int>(source.count(QStringLiteral("playbackScaleFactorPolicy = "))));
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts("));
    ASSERT_TRUE(facts.contains(QStringLiteral("playback_info_label::presentedScaleBelongsToRequest(")));
    ASSERT_TRUE(facts.contains(QStringLiteral("m_lastPresentedRequestContext.playbackScaleFactorPolicy")));
    // r6: each call site's whole argument list, anchored on its own statement (the contextValid bool, then the asked scale).
    ASSERT_TRUE(facts.contains(QStringLiteral(
        "facts.presentedValid = playback_info_label::presentedScaleBelongsToRequest(\n"
        "        m_lastPresentedRequestContextValid, m_lastPresentedRequestContext.playbackScaleFactorPolicy,\n"
        "        facts.requestedScale, activeScale );")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "const bool presentedBelongs = playback_info_label::presentedScaleBelongsToRequest(\n"
        "            m_lastPresentedRequestContextValid, m_lastPresentedRequestContext.playbackScaleFactorPolicy,\n"
        "            requestedScale, activeScale );")));
    ASSERT_FALSE(facts.contains(QStringLiteral("m_lastPresentedRequestContext.playbackScaleFactor ==")));
    const QString indicator = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackQualityIndicator("));
    ASSERT_TRUE(indicator.contains(QStringLiteral("playback_info_label::presentedScaleBelongsToRequest(")));
    ASSERT_FALSE(indicator.contains(QStringLiteral("m_lastPresentedRequestContext.playbackScaleFactor == requestedScale")));
    const QString key = functionBody(source, QStringLiteral("void MainWindow::refreshPlaybackInfoOnInputChange("));
    ASSERT_TRUE(key.contains(QStringLiteral("inputs.requestContextScale = m_lastPresentedRequestContext.playbackScaleFactorPolicy;")));
}

// r3 pass-2 A1 (a271daee): while playing the GPU window only STORES a changed label for the next frame's paint. Frames stall, FPS
// and DF are toggled off: the gated flush stores the text without the rate (repaintNow false), the reset then skips the withdrawal
// (the fps toggle is off), and the old measured rate stays on screen. Once frames stop (the meter's reset after a draw, while
// playing) every push repaints at once until the next draw, and the stall's entry pushes the stored label once, so a label stored
// for a frame that never comes is painted; the GPU window repaints an equal text that it has not drawn yet.
TEST(PlaybackInfoLabel, R3P2A1StalledFramesPaintTheStoredLabel)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString push = functionBody(source, QStringLiteral("void MainWindow::pushLookLabelOverlay("));
    ASSERT_TRUE(push.contains(QStringLiteral(
        "const bool repaintNow = !ui->actionPlay->isChecked() || m_lookLabelOverlayText.isEmpty() || m_playbackInfoRateWithdrawn"
        " || m_playbackFramesStalled;")));
    const QString tick = functionBody(source, QStringLiteral("void MainWindow::timerFrameEvent("));
    const int enteredAt = tick.indexOf(QStringLiteral("const bool stallEntered = ui->actionPlay->isChecked() && hasDraw;"));
    const int setAt = tick.indexOf(QStringLiteral("if( stallEntered ) m_playbackFramesStalled = true;"));
    const int pushAt = tick.indexOf(QStringLiteral("if( stallEntered && m_lookLabelFullscreenShown ) pushLookLabelOverlay();"));
    ASSERT_TRUE(enteredAt >= 0);
    ASSERT_TRUE(setAt > enteredAt);
    ASSERT_TRUE(pushAt > setAt);
    // Paused is not a stall (it repaints anyway), and the next draw ends the stall.
    ASSERT_TRUE(tick.contains(QStringLiteral("else if( !ui->actionPlay->isChecked() ) m_playbackFramesStalled = false;")));
    const int drawAt = tick.indexOf(QStringLiteral("lastDrawTime = nowTime;"));
    ASSERT_TRUE(drawAt >= 0);
    ASSERT_TRUE(tick.indexOf(QStringLiteral("m_playbackFramesStalled = false;"), drawAt) > drawAt);
    ASSERT_EQ(1, static_cast<int>(source.count(QStringLiteral("m_playbackFramesStalled = true;"))));
    const QString gpu = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString set = functionBody(gpu, QStringLiteral("bool GpuDisplayWindow::setLookLabelIfActive("));
    const int equalAt = set.indexOf(QStringLiteral("if ( win->m_lookLabelText == text )"));
    const int undrawnAt = set.indexOf(QStringLiteral("if ( repaintNow && win->m_lookLabelDrawnText != text ) win->update();"));
    ASSERT_TRUE(equalAt >= 0);
    ASSERT_TRUE(undrawnAt > equalAt);
}

// r3 pass-2 A2 (a271daee): LMMSE selected (use_amaze=4) passes the GPU AMaZE eligibility, GPU AMaZE renders the frame, and the
// label read "LMMSE (GPU)". The GPU debayer flag names what ran: "LMMSE->AMaZE (GPU)" (selected -> ran, the scale field's form).
TEST(PlaybackInfoLabel, R3P2A2GpuDebayerNamesWhatRan)
{
    playback_info_label::Facts f = ownerFacts();
    f.debayer = QStringLiteral("LMMSE");
    f.debayerOnGpu = true;
    f.gpuDebayer = QStringLiteral("AMaZE");
    ASSERT_QSTR_EQ(QStringLiteral("LMMSE->AMaZE (GPU)"), playback_info_label::debayerField(f));
    f.debayer = QStringLiteral("AMaZE");
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE (GPU)"), playback_info_label::debayerField(f));
    f.debayer = QStringLiteral("Bilinear");
    f.gpuDebayer = QStringLiteral("Bilinear");
    ASSERT_QSTR_EQ(QStringLiteral("Bilinear (GPU)"), playback_info_label::debayerField(f));
    f.debayer = QString();
    ASSERT_QSTR_EQ(QStringLiteral("Bilinear (GPU)"), playback_info_label::debayerField(f));
    // On the CPU: the selected name alone.
    f.debayer = QStringLiteral("LMMSE");
    f.debayerOnGpu = false;
    f.gpuDebayer = QString();
    ASSERT_QSTR_EQ(QStringLiteral("LMMSE"), playback_info_label::debayerField(f));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts("));
    ASSERT_TRUE(facts.contains(QStringLiteral(
        "facts.gpuDebayer = m_lastPresentedFrameUsedGpuAmazeDebayer ? QStringLiteral( \"AMaZE\" )")));
}

// r2 adversary A2 (deeae9f4, advisory): a carriage return is a line break too: logForm leaves one line, compose adds none.
TEST(PlaybackInfoLabel, R2A2CarriageReturnsNeverBreakALine)
{
    const QString log = playback_info_label::logForm(QStringLiteral("GPU\rCPU\r\nX\nY"));
    ASSERT_FALSE(log.contains(QLatin1Char('\r')));
    ASSERT_FALSE(log.contains(QLatin1Char('\n')));
    ASSERT_QSTR_EQ(QStringLiteral("GPU / CPU / X / Y"), log);
    playback_info_label::Facts f = ownerFacts();
    f.processingBadge = QStringLiteral("A\rB");
    const QString text = playback_info_label::compose(f, playback_info_label::Toggles(), true);
    ASSERT_FALSE(text.contains(QLatin1Char('\r')));
    ASSERT_EQ(2, static_cast<int>(text.split(QLatin1Char('\n')).size()));
}

// r1 adversary A2 (06898800): with RAW corrections off (fix_raw=0) llrawproc never runs, so the selected Dual-ISO mode is not
// applied: "Dual-ISO on" only while both are on, and the RAW corrections switch relabels.
TEST(PlaybackInfoLabel, R1A2DualIsoOnNeedsRawCorrections)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts("));
    // INFO-2 r2 (pass 1 A5): the field reads the displayed image's own state (playbackInfoDualIsoOn); the r1 A2 rule moved to the live reader
    ASSERT_TRUE(facts.contains(QStringLiteral("facts.dualIsoOn = playbackInfoDualIsoOn();")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "return m_pMlvObject && llrpGetFixRawMode( m_pMlvObject ) != 0 && llrpGetDualIsoMode( m_pMlvObject ) != 0;")));
    const QString rawFix = functionBody(source, QStringLiteral("void MainWindow::on_checkBoxRawFixEnable_clicked("));
    ASSERT_TRUE(rawFix.contains(QStringLiteral("updatePlaybackInfoLabel();")));
}

// r1 adversary A5 (06898800, advisory): a field holding a newline must not add a line: compose stays at two lines.
TEST(PlaybackInfoLabel, R1A5FieldNewlinesNeverAddLines)
{
    playback_info_label::Facts f = ownerFacts();
    f.processingBadge = QStringLiteral("A\nB\nC");
    f.debayer = QStringLiteral("X\nY");
    const QString text = playback_info_label::compose(f, playback_info_label::Toggles(), true);
    ASSERT_EQ(2, static_cast<int>(text.split(QLatin1Char('\n')).size()));
    ASSERT_TRUE(text.startsWith(QStringLiteral("GPU display A B C")));
    ASSERT_TRUE(text.contains(QStringLiteral("X Y (GPU)")));
}

// r7 adversary pass 10 A1 (235e3275): non-Dual-ISO clip, CPU playback at scale 2 with AMaZE selected. The scaled CPU route bins
// the Bayer (pl_downsample_bayer_to_rgb_2x, video_mlv.c) and never runs AMaZE, yet both surfaces read "AMaZE". A CPU frame below
// full scale names the scaled route; scales 4 and 8 share it; full scale and the GPU forms are unchanged.
TEST(PlaybackInfoLabel, R7P10A1ScaledCpuFrameNeverNamesTheSelectedDebayerAsRan)
{
    playback_info_label::Facts f = ownerFacts();
    f.gpuDisplay = false;
    f.processingBadge = QStringLiteral("CPU");
    f.dualIsoClip = false;
    f.debayer = QStringLiteral("AMaZE");
    f.debayerOnGpu = false;
    f.gpuDebayer = QString();
    for (int scale : {2, 4, 8}) {
        f.requestedScale = scale;
        f.presentedScale = scale;
        f.presentedValid = true;
        ASSERT_QSTR_EQ(QStringLiteral("AMaZE->Scaled"), playback_info_label::debayerField(f));
        const QString overlay = playback_info_label::compose(f, playback_info_label::Toggles(), true);
        const QString twin = playback_info_label::compose(f, playback_info_label::Toggles(), false);
        ASSERT_TRUE(overlay.contains(QStringLiteral("AMaZE->Scaled")));
        ASSERT_TRUE(twin.contains(QStringLiteral("AMaZE->Scaled")));
        ASSERT_FALSE(overlay.section(QLatin1Char('\n'), 0, 0).endsWith(dot() + QStringLiteral("AMaZE")));
    }
    // the scale the scale field shows: a presented Full frame of a Half request ran full scale, so the selected name stands
    f.requestedScale = 2;
    f.presentedScale = 1;
    f.presentedValid = true;
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE"), playback_info_label::debayerField(f));
    // no presented frame of this request yet: the requested scale is the one shown
    f.presentedValid = false;
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE->Scaled"), playback_info_label::debayerField(f));
    // full scale on the CPU: the selected debayer ran
    f.requestedScale = 1;
    f.presentedScale = 1;
    f.presentedValid = true;
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE"), playback_info_label::debayerField(f));
    // scale unknown (0): no claim beyond the selected name
    f.requestedScale = 0;
    f.presentedScale = 0;
    f.presentedValid = false;
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE"), playback_info_label::debayerField(f));
    // the GPU forms are unchanged at a reduced scale (the GPU names what it ran)
    f.requestedScale = 2;
    f.presentedScale = 2;
    f.presentedValid = true;
    f.debayerOnGpu = true;
    f.gpuDebayer = QStringLiteral("AMaZE");
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE (GPU)"), playback_info_label::debayerField(f));
    f.gpuDebayer = QString();
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE (GPU)"), playback_info_label::debayerField(f));
}

// r12 adversary A1 (ff6a22ab): 1920x1088 24-fps HQ Dual-ISO clip, CPU 8-bit preview, fast playback processing on, Full playback
// scale, Preview Resolution=Half, LMMSE selected. The x1 half/quarter proxy (video_mlv.c mlv_render_scaled_rgb16_x1_half_preview_core)
// debayers bilinear (debayerBasicU16) at reduced dimensions and upscales, while the output scale stays 1, so both surfaces read
// "LMMSE". The presented frame's own route code names what ran: codes 6/7 the half proxy, 9/10 the quarter proxy.
TEST(PlaybackInfoLabel, R12A1ReducedProxyFrameNamesTheProxyDebayerNotTheSelected)
{
    playback_info_label::Facts f = ownerFacts();
    f.gpuDisplay = false;
    f.processingBadge = QStringLiteral("CPU");
    f.debayer = QStringLiteral("LMMSE");
    f.debayerOnGpu = false;
    f.gpuDebayer = QString();
    f.requestedScale = 1;
    f.presentedScale = 1;
    f.presentedValid = true;
    f.dualIsoClip = true;
    f.dualIsoOn = true;
    f.width = 1920;
    f.height = 1088;
    struct RouteCase { int route; const char *expected; };
    const RouteCase cases[] = {
        { 6, "LMMSE->Bilinear (Half proxy)" },
        { 7, "LMMSE->Bilinear (Half proxy)" },
        { 9, "LMMSE->Bilinear (Quarter proxy)" },
        { 10, "LMMSE->Bilinear (Quarter proxy)" },
    };
    for (const RouteCase &c : cases) {
        f.presentedRoute = c.route;
        ASSERT_QSTR_EQ(QString::fromLatin1(c.expected), playback_info_label::debayerField(f));
        const QString overlay = playback_info_label::compose(f, playback_info_label::Toggles(), true);
        const QString twin = playback_info_label::compose(f, playback_info_label::Toggles(), false);
        const QString overlayLineA = overlay.section(QLatin1Char('\n'), 0, 0);
        const QString twinLineA = twin.section(QLatin1Char('\n'), 0, 0);
        ASSERT_FALSE(overlayLineA.endsWith(dot() + QStringLiteral("LMMSE")));
        ASSERT_FALSE(twinLineA.endsWith(dot() + QStringLiteral("LMMSE")));
        ASSERT_TRUE(overlayLineA.endsWith(QString::fromLatin1(c.expected)));
        ASSERT_TRUE(twinLineA.endsWith(QString::fromLatin1(c.expected)));
    }
    // the full-recon route (0) and a code that is no x1 proxy leave the selected name standing at full scale
    for (int route : {0, 2, 3, 4, 8, -1, 99}) {
        f.presentedRoute = route;
        ASSERT_QSTR_EQ(QStringLiteral("LMMSE"), playback_info_label::debayerField(f));
    }
    // Bilinear selected: the proxy ran the selected algorithm, at the proxy's resolution
    f.presentedRoute = 6;
    f.debayer = QStringLiteral("Bilinear");
    ASSERT_QSTR_EQ(QStringLiteral("Bilinear (Half proxy)"), playback_info_label::debayerField(f));
    // no selected name known: the proxy still names what ran
    f.debayer = QString();
    f.presentedRoute = 9;
    ASSERT_QSTR_EQ(QStringLiteral("Bilinear (Quarter proxy)"), playback_info_label::debayerField(f));
    // the GPU forms are unchanged (the GPU names what it ran)
    f.debayer = QStringLiteral("LMMSE");
    f.presentedRoute = 6;
    f.debayerOnGpu = true;
    f.gpuDebayer = QStringLiteral("AMaZE");
    ASSERT_QSTR_EQ(QStringLiteral("LMMSE->AMaZE (GPU)"), playback_info_label::debayerField(f));
}

// r12 adversary A1 siblings (MainWindow.cpp playbackInfoFacts, refreshPlaybackInfoOnInputChange): the route is read from the
// presented frame's own telemetry, and only from a frame whose processed stage ran (the tag is thread-local and is not reset by
// the GPU debayered routes); a route change relabels like any other info input.
TEST(PlaybackInfoLabel, R12A1PresentedRouteIsReadFromThePresentedFrame)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts("));
    ASSERT_TRUE(facts.contains(QStringLiteral("facts.presentedRoute = presentedPlaybackRouteCode();")));
    const QString route = functionBody(source, QStringLiteral("int MainWindow::presentedPlaybackRouteCode("));
    ASSERT_TRUE(route.contains(QStringLiteral("m_lastPresentedStageTimingTelemetry")));
    ASSERT_TRUE(route.contains(QStringLiteral("\"processed16_stage_measured\"")));
    ASSERT_TRUE(route.contains(QStringLiteral("\"render_thread_phase4b_path\"")));
    ASSERT_FALSE(route.contains(QStringLiteral("mlv_phase4bv2_last_path_taken")));
    const QString inputs = functionBody(source, QStringLiteral("void MainWindow::refreshPlaybackInfoOnInputChange("));
    ASSERT_TRUE(inputs.contains(QStringLiteral("inputs.presentedRoute = presentedPlaybackRouteCode();")));
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("presentedRoute == o.presentedRoute")));
}

// r12 adversary pass 13 A1 (c58f5bd8): ordinary 1920x1088 clip, CPU presentation, receipt AMaZE with Don't Switch Debayer, fast
// playback processing on, MLVAPP_PLAYBACK_SCALE_FACTOR=2, Zoom Fit off. OutputDebayered16 reconstructs full-size AMaZE, the prep
// refuses the reduction (factor 1), and the render thread reports the requested 2: both surfaces read "Half" and
// "AMaZE->Scaled" over a full-resolution AMaZE frame. A CPU prep frame is shown at the render's achieved size times the prep factor.
TEST(PlaybackInfoLabel, R12P13A1CpuPrepFrameNamesTheScaleItWasShownAt)
{
    // the refused prep: full-size render (1920 of 1920), factor 1 -> Full, though the render thread reported 2
    ASSERT_EQ(1, playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 1920, 1));
    // the prep's own reduction of a full-size render, and of a render that consumed a reduced recon
    ASSERT_EQ(2, playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 1920, 2));
    ASSERT_EQ(4, playback_info_label::presentedScaleOfCpuPrepRoute(4, 1920, 1920, 4));
    ASSERT_EQ(2, playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 960, 1));
    ASSERT_EQ(4, playback_info_label::presentedScaleOfCpuPrepRoute(4, 1920, 960, 2));
    // unreadable or impossible inputs keep the render's own report
    ASSERT_EQ(2, playback_info_label::presentedScaleOfCpuPrepRoute(2, 0, 1920, 1));
    ASSERT_EQ(2, playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 0, 1));
    ASSERT_EQ(2, playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 3840, 1));
    ASSERT_EQ(2, playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 1920, 0));
    ASSERT_EQ(2, playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 1920, 3));
    ASSERT_EQ(8, playback_info_label::presentedScaleOfCpuPrepRoute(8, 1920, 1920, 16));
    // the finding's label: requested Half, shown Full -> "Half->Full" and the selected AMaZE ran at full size
    playback_info_label::Facts f = ownerFacts();
    f.gpuDisplay = false;
    f.processingBadge = QStringLiteral("CPU");
    f.dualIsoClip = false;
    f.debayer = QStringLiteral("AMaZE");
    f.debayerOnGpu = false;
    f.gpuDebayer = QString();
    f.requestedScale = 2;
    f.presentedScale = playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 1920, 1);
    f.presentedValid = playback_info_label::presentedScaleBelongsToRequest(true, 2, f.requestedScale, f.presentedScale);
    ASSERT_QSTR_EQ(QStringLiteral("Half->Full"), playback_info_label::scaleField(f));
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE"), playback_info_label::debayerField(f));
    // the MainWindow sites read the one accessor (label facts, relabel inputs, toolbar indicator)
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString shown = functionBody(source, QStringLiteral("int MainWindow::presentedPlaybackScaleShown("));
    ASSERT_TRUE(shown.contains(QStringLiteral("\"playback_prep_cpu_route_active\"")));
    ASSERT_TRUE(shown.contains(QStringLiteral("\"render_thread_source_width\"")));
    ASSERT_TRUE(shown.contains(QStringLiteral("\"render_thread_rendered_width\"")));
    ASSERT_TRUE(shown.contains(QStringLiteral("\"playback_prep_reduced_factor\"")));
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts("));
    ASSERT_TRUE(facts.contains(QStringLiteral("const int activeScale = presentedPlaybackScaleShown();")));
    const QString indicator = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackQualityIndicator("));
    ASSERT_TRUE(indicator.contains(QStringLiteral("const int activeScale = presentedPlaybackScaleShown();")));
    ASSERT_FALSE(indicator.contains(QStringLiteral("const int activeScale = m_lastPresentedPlaybackScaleFactorActive;")));
}

// r12 adversary pass 14 A1 (b8b8f430): Quality mode, MLVAPP_PLAYBACK_SCALE_FACTOR=2, CPU prep, Zoom Fit off then on. Both frames
// report active scale 2 and render 1920 wide; the prep factor goes 1 -> 2. The indicator cache compared the raw active scale (2
// both times) and returned before the corrected accessor ran, so the toolbar kept "Half res->Full res" over a Half frame.
// The cache keys on the scale the indicator shows, the same accessor its label reads.
TEST(PlaybackInfoLabel, R12P14A1IndicatorCacheKeysOnTheShownScale)
{
    // the two frames of the finding: same raw report, different shown scale
    ASSERT_EQ(1, playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 1920, 1));
    ASSERT_EQ(2, playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 1920, 2));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString indicator = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackQualityIndicator("));
    const int cacheAt = indicator.indexOf(QStringLiteral("const PlaybackQualityIndicatorCache currentCache"));
    ASSERT_TRUE(cacheAt >= 0);
    const int cacheEnd = indicator.indexOf(QStringLiteral("};"), cacheAt);
    ASSERT_TRUE(cacheEnd > cacheAt);
    const QString cache = indicator.mid(cacheAt, cacheEnd - cacheAt);
    ASSERT_TRUE(cache.contains(QStringLiteral("presentedPlaybackScaleShown(),")));
    ASSERT_FALSE(cache.contains(QStringLiteral("m_lastPresentedPlaybackScaleFactorActive")));
    // the cache compare still reads that field, so a shown-scale change misses the cache and relabels
    ASSERT_TRUE(indicator.contains(QStringLiteral("currentCache.lastPresentedPlaybackScaleFactorActive")));
}

// r12 adversary pass 13 A2 (c58f5bd8): previous Play EMA 40 ms, last draw at .000, Stop at .100, Play at .450, first new draw at
// .458. The first draw smoothed the 458 ms cross-Play gap into the old average (81.8 ms, 12.2 fps), so the relabel near .700
// showed a rate though only one draw belonged to this Play. The first draw of a Play feeds no interval; the next one seeds it.
TEST(PlaybackInfoLabel, R12P13A2NewPlayRateWaitsForAnIntervalInsideThisPlay)
{
    double ema = 40.0;                       // the previous Play's average
    // on_actionPlay_toggled drops the average; the first draw of the new Play feeds nothing
    ema = 0.0;
    int interval = playback_fps_meter::meterIntervalMs(true, true, 458);
    ASSERT_EQ(0, interval);
    ema = playback_fps_meter::smoothedFrameMs(ema, interval);
    ASSERT_TRUE(ema <= 0.0);                 // unmeasured: the corner reads "--"
    // the second draw, 40 ms later inside this Play, seeds the average from its own interval
    interval = playback_fps_meter::meterIntervalMs(false, true, 40);
    ASSERT_EQ(40, interval);
    ema = playback_fps_meter::smoothedFrameMs(ema, interval);
    ASSERT_TRUE(ema > 39.9 && ema < 40.1);
    // no previous draw at all: nothing to measure
    interval = playback_fps_meter::meterIntervalMs(false, false, 123);
    ASSERT_EQ(0, interval);
    // the wiring: the first draw after a toggle consumes the flag, drops the average and feeds meterIntervalMs
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int drawAt = source.indexOf(QStringLiteral("const bool firstDrawOfPlay = m_playbackFpsMeterNewPlay;"));
    ASSERT_TRUE(drawAt >= 0);
    const QString draw = source.mid(drawAt, 600);
    ASSERT_TRUE(draw.contains(QStringLiteral("m_playbackFpsMeterNewPlay = false;")));
    ASSERT_TRUE(draw.contains(QStringLiteral("if( firstDrawOfPlay ) m_playbackFpsEmaFrameMs = 0.0;")));
    ASSERT_TRUE(draw.contains(QStringLiteral("playback_fps_meter::meterIntervalMs(")));
}

// r7 adversary pass 11 A1 (045434fc): Stop at t=100 ms, Play at t=450 ms with no idle tick between: the corner kept the last
// Play's measured 23.8 (only the > 500 ms idle reset zeroed it) and composed "23.8/24 fps" before this Play measured a frame.
// The Play toggle drops the snapshot BEFORE the relabel it triggers, so a new Play reads "--" until its own measurement.
TEST(PlaybackInfoLabel, R7P11A1APlayToggleDropsThePreviousPlaysRate)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString toggled = functionBody(source, QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"));
    ASSERT_FALSE(toggled.isEmpty());
    // r12 (pass 13 A2): the drop still precedes the relabel it triggers; the meter re-arm sits between them
    const int dropAt = toggled.indexOf(QStringLiteral("m_playbackInfoFpsShown = 0.0;"));
    ASSERT_TRUE(dropAt >= 0);
    const int relabelAt = toggled.indexOf(QStringLiteral("updatePlaybackInfoLabel();"), dropAt);
    ASSERT_TRUE(relabelAt > dropAt);
    const QString between = toggled.mid(dropAt, relabelAt - dropAt);
    ASSERT_TRUE(between.contains(QStringLiteral("m_playbackFpsEmaFrameMs = 0.0;")));
    ASSERT_TRUE(between.contains(QStringLiteral("m_playbackFpsMeterNewPlay = true;")));
    // the composed form for a new Play with no snapshot is "--/24 fps"
    playback_info_label::Facts f = ownerFacts();
    f.playing = true;
    f.achievedFps = 0.0;
    ASSERT_QSTR_EQ(QStringLiteral("--/24 fps"), playback_info_label::fpsField(f));
}

namespace
{

// The DEFINITION of a C function (video_mlv.c forward-declares some, and a declaration ends in ';' before any '{').
QString definitionBody(const QString &source, const QString &signature)
{
    int at = source.indexOf(signature);
    while ( at >= 0 ) {
        const int brace = source.indexOf(QLatin1Char('{'), at);
        const int semi = source.indexOf(QLatin1Char(';'), at);
        if ( brace >= 0 && ( semi < 0 || brace < semi ) ) {
            const int end = source.indexOf(QStringLiteral("\n}\n"), at);
            return source.mid(at, end > at ? end - at : -1);
        }
        at = source.indexOf(signature, at + signature.size());
    }
    ASSERT_TRUE(false);
    return QString();
}

// The presented facts of a CPU 8-bit frame at Full scale that passes every route guard.
playback_info_label::PresentedRouteFacts cpuScale1RouteFacts(int routeTag)
{
    playback_info_label::PresentedRouteFacts r;
    r.contextValid = true;
    r.activeScale = 1;
    r.gpuDebayer = false;
    r.processedStageMeasured = true;
    r.direct8 = false;
    r.gpuRecon = false;
    r.routeTag = routeTag;
    return r;
}

playback_info_label::Facts cpuFullScaleFacts(const QString &selected)
{
    playback_info_label::Facts f = ownerFacts();
    f.gpuDisplay = false;
    f.processingBadge = QStringLiteral("CPU");
    f.debayer = selected;
    f.debayerOnGpu = false;
    f.gpuDebayer = QString();
    f.requestedScale = 1;
    f.presentedScale = 1;
    f.presentedValid = true;
    return f;
}

} // namespace

// r13 keys B1 (opus key3, fable, sol on 866b3422), catalogue row L-07 (label-truth): M16 HQ Dual-ISO clip, Full scale, Preview
// Resolution Auto. Play: the x1 proxy writes route 6 on the render thread. Set Preview Resolution Full: the proxy no longer
// engages and direct-8 renders each frame at full resolution with the selected AMaZE, but the scale-1 direct-8 render never
// wrote the thread-local tag, so each frame was stored in the processed-8 cache with the stale 6. Enter fullscreen: the same-frame
// redraw is a cache hit (processed8_direct_path_active=false, processed16_stage_measured=true), the cache replays 6, and both
// surfaces read "AMaZE->Bilinear (Half proxy)" over a full-resolution AMaZE frame. The engine is not linked into console_tests,
// so the engine half is pinned as source text (the reset on each scale-1 8-bit route, before the cache lookup and the render that
// the stores read) and the label half runs on the replayed facts: the cache now replays route 0, and the label names AMaZE.
TEST(PlaybackInfoLabel, R13B1CachedDirect8FrameReplaysRouteZeroNotAnOlderProxy)
{
    const QString engine = readRepoFile(QStringLiteral("src/mlv/video_mlv.c"));
    const QString reset = QStringLiteral("g_mlv_phase4bv2_path_taken = 0;");
    // (1) the playback entry clears the tag at scale 1 before the cache lookup and before the direct-8 render and store
    const QString entry = definitionBody(engine, QStringLiteral("static void getMlvProcessedFrame8_with_scale("));
    const int guardAt = entry.indexOf(QStringLiteral("if (normalizedScale <= 1)\n    {\n        ") + reset);
    ASSERT_TRUE(guardAt >= 0);
    const int lookupAt = entry.indexOf(QStringLiteral("mlv_processed_frame_8bit_cache_try_copy("));
    const int renderAt = entry.indexOf(QStringLiteral("mlv_render_processed_frame8_direct_with_processing(video,"));
    const int storeAt = entry.indexOf(QStringLiteral("const int stored_phase4b_path = mlv_phase4bv2_last_path_taken();"));
    ASSERT_TRUE(lookupAt > guardAt);
    ASSERT_TRUE(renderAt > guardAt);
    ASSERT_TRUE(storeAt > renderAt);
    // (2) the direct-8 render clears it in its scale-1 branch, before the full-resolution debayer (the prefetch worker stores
    // its own thread's tag after calling it, so the entry reset alone would not cover the worker)
    const QString direct = definitionBody(engine, QStringLiteral("static int mlv_render_processed_frame8_direct_with_processing(mlvObject_t * video,"));
    const int scale1At = direct.indexOf(QStringLiteral("get_mlv_raw_frame_debayered(video,"));
    ASSERT_TRUE(scale1At >= 0);
    const int elseAt = direct.lastIndexOf(QStringLiteral("else\n    {"), scale1At);
    ASSERT_TRUE(elseAt >= 0);
    ASSERT_TRUE(direct.mid(elseAt, scale1At - elseAt).contains(reset));
    // (3) the class sibling: the reconned-raw scale-1 render (its caller stores the tag too) clears it before its debayer
    const QString reconned = definitionBody(engine, QStringLiteral("static int mlv_render_processed_frame8_direct_with_processing_from_reconned_raw("));
    const int eqAt = reconned.indexOf(QStringLiteral("if (eff_scale == 1)"));
    const int basicAt = reconned.indexOf(QStringLiteral("debayerBasicU16RecordingKernelMilliseconds("), eqAt);
    ASSERT_TRUE(eqAt >= 0);
    ASSERT_TRUE(basicAt > eqAt);
    ASSERT_TRUE(reconned.mid(eqAt, basicAt - eqAt).contains(reset));
    // (4) the scale > 1 routes keep their tag (it picks the x4/x8 Cubic resize): the entry reset is guarded by scale
    ASSERT_FALSE(entry.mid(0, guardAt).contains(reset));

    // the replayed facts of the fullscreen redraw: a cache hit at Full scale, direct-8 flag clear, processed stage measured
    playback_info_label::PresentedRouteFacts replay = cpuScale1RouteFacts(0);
    playback_info_label::Facts f = cpuFullScaleFacts(QStringLiteral("AMaZE"));
    f.dualIsoClip = true;
    f.dualIsoOn = true;
    f.presentedRoute = playback_info_label::presentedRouteOfFrame(replay);
    ASSERT_EQ(0, f.presentedRoute);
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE"), playback_info_label::debayerField(f));
    const QString overlay = playback_info_label::compose(f, playback_info_label::Toggles(), true);
    const QString twin = playback_info_label::compose(f, playback_info_label::Toggles(), false);
    ASSERT_FALSE(overlay.contains(QStringLiteral("proxy")));
    ASSERT_FALSE(twin.contains(QStringLiteral("proxy")));
    ASSERT_TRUE(overlay.section(QLatin1Char('\n'), 0, 0).endsWith(dot() + QStringLiteral("AMaZE")));
    ASSERT_TRUE(twin.section(QLatin1Char('\n'), 0, 0).endsWith(dot() + QStringLiteral("AMaZE")));
    // the label trusts a replayed tag that passes every guard, which is why the engine must replay 0: the pre-r13 replay of 6
    replay.routeTag = 6;
    f.presentedRoute = playback_info_label::presentedRouteOfFrame(replay);
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE->Bilinear (Half proxy)"), playback_info_label::debayerField(f));
}

// r13 hardening H-a (key3 H2): R12A1PresentedRouteIsReadFromThePresentedFrame pinned only processed16_stage_measured and the
// tag key; deleting the direct-8, GPU-recon or scale guard kept it green. The guards now live in the pure presentedRouteOfFrame,
// and each one fails a case below when it is deleted; the MainWindow accessor is pinned to feed each guard its own telemetry key.
TEST(PlaybackInfoLabel, R13HaEveryPresentedRouteGuardIsLoadBearing)
{
    // a fresh x1 proxy frame passes every guard and names its route
    ASSERT_EQ(6, playback_info_label::presentedRouteOfFrame(cpuScale1RouteFacts(6)));
    ASSERT_EQ(9, playback_info_label::presentedRouteOfFrame(cpuScale1RouteFacts(9)));
    // each guard alone withdraws the route
    playback_info_label::PresentedRouteFacts r = cpuScale1RouteFacts(6);
    r.direct8 = true;                           // a foreground direct-8 frame: full resolution, the selected debayer
    ASSERT_EQ(0, playback_info_label::presentedRouteOfFrame(r));
    r = cpuScale1RouteFacts(6);
    r.gpuRecon = true;                          // the GPU playback recon produced it
    ASSERT_EQ(0, playback_info_label::presentedRouteOfFrame(r));
    for (int scale : {0, 2, 4, 8}) {             // the x1 proxy cores run only at scale 1
        r = cpuScale1RouteFacts(6);
        r.activeScale = scale;
        ASSERT_EQ(0, playback_info_label::presentedRouteOfFrame(r));
    }
    r = cpuScale1RouteFacts(6);
    r.gpuDebayer = true;                        // GPU AMaZE or GPU bilinear debayered it
    ASSERT_EQ(0, playback_info_label::presentedRouteOfFrame(r));
    r = cpuScale1RouteFacts(6);
    r.processedStageMeasured = false;           // the CPU processed stage did not run (OutputDebayered16)
    ASSERT_EQ(0, playback_info_label::presentedRouteOfFrame(r));
    r = cpuScale1RouteFacts(6);
    r.contextValid = false;                     // no presented request context
    ASSERT_EQ(0, playback_info_label::presentedRouteOfFrame(r));
    // the label for the direct-8 case names the selected debayer
    r = cpuScale1RouteFacts(6);
    r.direct8 = true;
    playback_info_label::Facts f = cpuFullScaleFacts(QStringLiteral("LMMSE"));
    f.presentedRoute = playback_info_label::presentedRouteOfFrame(r);
    ASSERT_QSTR_EQ(QStringLiteral("LMMSE"), playback_info_label::debayerField(f));
    // the wiring: each guard reads its own key of the presented frame's telemetry, and the accessor returns the pure decision
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString route = functionBody(source, QStringLiteral("int MainWindow::presentedPlaybackRouteCode("));
    ASSERT_TRUE(route.contains(QStringLiteral("route.contextValid = m_lastPresentedRequestContextValid;")));
    ASSERT_TRUE(route.contains(QStringLiteral("route.activeScale = m_lastPresentedPlaybackScaleFactorActive;")));
    ASSERT_TRUE(route.contains(QStringLiteral("route.gpuDebayer = m_lastPresentedFrameUsedGpuAmazeDebayer || m_lastPresentedFrameUsedGpuBilinearDebayer;")));
    ASSERT_TRUE(route.contains(QStringLiteral("route.processedStageMeasured = telemetryBoolValue( telemetry, \"processed16_stage_measured\" );")));
    ASSERT_TRUE(route.contains(QStringLiteral("route.direct8 = telemetryBoolValue( telemetry, \"processed8_direct_path_active\" );")));
    ASSERT_TRUE(route.contains(QStringLiteral("route.gpuRecon = telemetryBoolValue( telemetry, \"gpu_playback_recon_used\" );")));
    ASSERT_TRUE(route.contains(QStringLiteral("route.routeTag = telemetryIntValue( telemetry, \"render_thread_phase4b_path\" );")));
    ASSERT_TRUE(route.contains(QStringLiteral("return playback_info_label::presentedRouteOfFrame( route );")));
}

// r13 adversary pass 15 A1 (189dd4f9): 1920x1088 ordinary clip, receipt AMaZE, Don't Switch Debayer, CPU preview processing,
// fast playback processing on, playback scale 2 (sibling: 4), Zoom Fit on, MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE=1. The reuse
// refusal debayers full-size AMaZE and the prep box-reduces the RGB by 2, so the frame is shown at Half, but AMaZE ran: both
// surfaces read "AMaZE->Scaled". A prep frame's debayer scale is the render's achieved size, never the prep's reduction after it.
TEST(PlaybackInfoLabel, R13P15A1PrepFrameDebayeredFullSizeNamesTheSelectedDebayer)
{
    // the render achieved full size: the debayer ran at 1; a reduced recon (960 of 1920) ran at 2; unreadable inputs are unknown
    ASSERT_EQ(1, playback_info_label::debayerScaleOfCpuPrepRoute(1920, 1920));
    ASSERT_EQ(2, playback_info_label::debayerScaleOfCpuPrepRoute(1920, 960));
    ASSERT_EQ(4, playback_info_label::debayerScaleOfCpuPrepRoute(1920, 480));
    ASSERT_EQ(0, playback_info_label::debayerScaleOfCpuPrepRoute(0, 1920));
    ASSERT_EQ(0, playback_info_label::debayerScaleOfCpuPrepRoute(1920, 0));
    ASSERT_EQ(0, playback_info_label::debayerScaleOfCpuPrepRoute(1920, 3840));
    ASSERT_EQ(0, playback_info_label::debayerScaleOfCpuPrepRoute(1920, 640));
    for (int scale : {2, 4}) {
        playback_info_label::Facts f = cpuFullScaleFacts(QStringLiteral("AMaZE"));
        f.dualIsoClip = false;
        f.requestedScale = scale;
        f.presentedScale = playback_info_label::presentedScaleOfCpuPrepRoute(scale, 1920, 1920, scale);
        ASSERT_EQ(scale, f.presentedScale);
        f.presentedValid = playback_info_label::presentedScaleBelongsToRequest(true, scale, f.requestedScale, f.presentedScale);
        f.debayerScale = playback_info_label::debayerScaleOfCpuPrepRoute(1920, 1920);
        ASSERT_QSTR_EQ(QStringLiteral("AMaZE"), playback_info_label::debayerField(f));
        const QString overlay = playback_info_label::compose(f, playback_info_label::Toggles(), true);
        const QString twin = playback_info_label::compose(f, playback_info_label::Toggles(), false);
        ASSERT_FALSE(overlay.contains(QStringLiteral("->Scaled")));
        ASSERT_FALSE(twin.contains(QStringLiteral("->Scaled")));
        ASSERT_TRUE(overlay.section(QLatin1Char('\n'), 0, 0).endsWith(dot() + QStringLiteral("AMaZE")));
        // a prep frame that consumed a reduced recon still reads "->Scaled", and so does a frame with no debayer scale
        f.debayerScale = playback_info_label::debayerScaleOfCpuPrepRoute(1920, 1920 / scale);
        ASSERT_QSTR_EQ(QStringLiteral("AMaZE->Scaled"), playback_info_label::debayerField(f));
        f.debayerScale = 0;
        ASSERT_QSTR_EQ(QStringLiteral("AMaZE->Scaled"), playback_info_label::debayerField(f));
    }
    // the wiring: the label facts and the relabel inputs read the one accessor, and the accessor reads the prep frame's sizes
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString accessor = functionBody(source, QStringLiteral("int MainWindow::presentedPlaybackDebayerScale("));
    ASSERT_TRUE(accessor.contains(QStringLiteral("\"playback_prep_cpu_route_active\"")));
    ASSERT_TRUE(accessor.contains(QStringLiteral("\"render_thread_source_width\"")));
    ASSERT_TRUE(accessor.contains(QStringLiteral("\"render_thread_rendered_width\"")));
    ASSERT_TRUE(accessor.contains(QStringLiteral("playback_info_label::debayerScaleOfCpuPrepRoute(")));
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts("));
    ASSERT_TRUE(facts.contains(QStringLiteral("facts.debayerScale = presentedPlaybackDebayerScale();")));
    const QString inputs = functionBody(source, QStringLiteral("void MainWindow::refreshPlaybackInfoOnInputChange("));
    ASSERT_TRUE(inputs.contains(QStringLiteral("inputs.debayerScale = presentedPlaybackDebayerScale();")));
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("debayerScale == o.debayerScale")));
}

// r13 adversary pass 15 A2 (189dd4f9): fullscreen GPU window, fps on, 24-fps clip. The previous Play drew, then stalled > 500 ms
// (m_playbackFramesStalled set, lastDrawTime dropped); Stop at 610 ms, Play at 910 ms with no tick between. Neither toggle cleared
// the stall, so the new Play's "--/24 fps" relabel pushed with repaintNow=true and the GPU window painted the retained frame. The
// Play toggle clears the stall flag BEFORE the relabel it triggers; a stall of the new Play is set again after its own draw.
TEST(PlaybackInfoLabel, R13P15A2APlayToggleClearsThePreviousPlaysStall)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString toggled = functionBody(source, QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"));
    ASSERT_FALSE(toggled.isEmpty());
    const int clearAt = toggled.indexOf(QStringLiteral("m_playbackFramesStalled = false;"));
    ASSERT_TRUE(clearAt >= 0);
    const int relabelAt = toggled.indexOf(QStringLiteral("updatePlaybackInfoLabel();"));
    ASSERT_TRUE(relabelAt > clearAt);
    // the push reads the flag for its repaint decision, and only an in-Play stall after a draw sets it again
    const QString push = functionBody(source, QStringLiteral("void MainWindow::pushLookLabelOverlay( void )"));
    ASSERT_TRUE(push.contains(QStringLiteral("m_playbackFramesStalled;")));
    ASSERT_TRUE(source.contains(QStringLiteral("const bool stallEntered = ui->actionPlay->isChecked() && hasDraw;")));
    ASSERT_TRUE(source.contains(QStringLiteral("if( stallEntered ) m_playbackFramesStalled = true;")));
}

// r13 adversary pass 16 A1 (deb5a42b): ordinary 1920x1088 AMaZE receipt, Don't Switch Debayer, MLVAPP_EXPERIMENTAL_GL_VIEWPORT=1,
// GPU preview processing, CPU debayer backends, GPU recon texture route off, MLVAPP_PLAYBACK_SCALE_FACTOR=2,
// MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE=1. OutputDebayered16 debayers full-size CPU AMaZE and the GPU prep presents it at 1920
// wide, but the render thread reports the requested 2 and the frame is not on the CPU prep route: both surfaces read "Half" and
// "AMaZE->Scaled". Such a frame is shown, and was debayered, at the size the render achieved: "Half->Full" and "AMaZE".
TEST(PlaybackInfoLabel, R13P16A1GpuPrepFrameDebayeredFullSizeNamesFullAndTheSelectedDebayer)
{
    // the finding's frame: no CPU processed stage, no GPU recon, no GPU debayer, rendered 1920 of 1920 -> 1
    ASSERT_EQ(1, playback_info_label::unreducedCpuDebayerScale(false, false, 1920, 1920));
    // a reduced recon consumed by the render (960 of 1920) -> 2
    ASSERT_EQ(2, playback_info_label::unreducedCpuDebayerScale(false, false, 1920, 960));
    // any other frame keeps the render's report (0): a CPU processed frame, a Bayer-texture-presented frame (r16: not the GPU
    // recon as such, which a read-back CPU debayer follows, nor a GPU debayer, which debayers the source size), unreadable sizes
    ASSERT_EQ(0, playback_info_label::unreducedCpuDebayerScale(true, false, 1920, 1920));
    ASSERT_EQ(0, playback_info_label::unreducedCpuDebayerScale(false, true, 1920, 1920));
    ASSERT_EQ(0, playback_info_label::unreducedCpuDebayerScale(false, false, 0, 1920));
    ASSERT_EQ(0, playback_info_label::unreducedCpuDebayerScale(false, false, 1920, 0));
    for (int scale : {2, 4}) {
        playback_info_label::Facts f = cpuFullScaleFacts(QStringLiteral("AMaZE"));
        f.processingBadge = QStringLiteral("GPU Preview");
        f.gpuDisplay = true;
        f.dualIsoClip = false;
        f.requestedScale = scale;
        const int shown = playback_info_label::unreducedCpuDebayerScale(false, false, 1920, 1920);
        f.presentedScale = shown > 0 ? shown : scale;
        f.presentedValid = playback_info_label::presentedScaleBelongsToRequest(true, scale, f.requestedScale, f.presentedScale);
        f.debayerScale = shown;
        ASSERT_QSTR_EQ(QString(PlaybackScaleLabel::fractionWord(scale)) + QStringLiteral("->Full"), playback_info_label::scaleField(f));
        ASSERT_QSTR_EQ(QStringLiteral("AMaZE"), playback_info_label::debayerField(f));
        const QString overlay = playback_info_label::compose(f, playback_info_label::Toggles(), true);
        ASSERT_FALSE(overlay.contains(QStringLiteral("->Scaled")));
    }
    // the wiring: the shown scale and the debayer scale both read the one accessor for a frame off the CPU prep route
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString unreduced = functionBody(source, QStringLiteral("int MainWindow::presentedPlaybackUnreducedCpuDebayerScale("));
    ASSERT_TRUE(unreduced.contains(QStringLiteral("playback_info_label::unreducedCpuDebayerScale(")));
    ASSERT_TRUE(unreduced.contains(QStringLiteral("telemetryBoolValue( telemetry, \"processed16_stage_measured\" ),")));
    ASSERT_TRUE(unreduced.contains(QStringLiteral("telemetryBoolValue( telemetry, \"gpu_playback_recon_texture_present_active\" ),")));
    ASSERT_FALSE(unreduced.contains(QStringLiteral("m_lastPresentedFrameUsedGpuAmazeDebayer")));   // r16 pass 18 A1
    ASSERT_TRUE(unreduced.contains(QStringLiteral("telemetryIntValue( telemetry, \"render_thread_source_width\" ),")));
    ASSERT_TRUE(unreduced.contains(QStringLiteral("telemetryIntValue( telemetry, \"render_thread_rendered_width\" ) );")));
    const QString shownBody = functionBody(source, QStringLiteral("int MainWindow::presentedPlaybackScaleShown("));
    ASSERT_TRUE(shownBody.contains(QStringLiteral("const int unreduced = presentedPlaybackUnreducedCpuDebayerScale();")));
    ASSERT_TRUE(shownBody.contains(QStringLiteral("return unreduced > 0 ? unreduced : m_lastPresentedPlaybackScaleFactorActive;")));
    const QString debayerBody = functionBody(source, QStringLiteral("int MainWindow::presentedPlaybackDebayerScale("));
    ASSERT_TRUE(debayerBody.contains(QStringLiteral("return presentedPlaybackUnreducedCpuDebayerScale();")));
}

// r16 (adversary pass 17 A1 on 06faad35): 1920x1088 HQ Dual-ISO, RAW corrections on, AMaZE, Don't Switch Debayer, GL surface,
// GPU preview processing, CPU debayer backends, MLVAPP_GPU_PLAYBACK_RECON=1, texture presentation OFF, MLVAPP_PLAYBACK_SCALE_FACTOR=2,
// MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE=1; a supported CUDA recon reads back. Tuple: processedStageMeasured=false, gpuRecon=true,
// gpuDebayer=false, 1920 of 1920, active 2. The CUDA recon is read back and the CPU debayers it full-size AMaZE, yet the helper
// refused the frame for gpuRecon alone: both surfaces read "Half" and "AMaZE->Scaled". A GPU recon is not a GPU debayer; only a
// frame the viewport presented as a Bayer texture (gpu_playback_recon_texture_present_active) skipped the CPU debayer.
TEST(PlaybackInfoLabel, R16P17A1ReadbackGpuReconFrameDebayeredFullSizeNamesFullAndTheSelectedDebayer)
{
    // the finding's frame: not texture-presented, no CPU processed stage, no GPU debayer, rendered 1920 of 1920 -> 1
    const int shown = playback_info_label::unreducedCpuDebayerScale(false, false, 1920, 1920);
    ASSERT_EQ(1, shown);
    // a frame the viewport presented as a Bayer texture keeps the render's report
    ASSERT_EQ(0, playback_info_label::unreducedCpuDebayerScale(false, true, 1920, 1920));
    playback_info_label::Facts f = cpuFullScaleFacts(QStringLiteral("AMaZE"));
    f.processingBadge = QStringLiteral("GPU Preview");
    f.gpuDisplay = true;
    f.requestedScale = 2;
    f.presentedScale = shown;
    f.presentedValid = playback_info_label::presentedScaleBelongsToRequest(true, 2, f.requestedScale, f.presentedScale);
    f.debayerScale = shown;
    ASSERT_QSTR_EQ(QStringLiteral("Half->Full"), playback_info_label::scaleField(f));
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE"), playback_info_label::debayerField(f));
    const QString overlay = playback_info_label::compose(f, playback_info_label::Toggles(), true);
    const QString twin = playback_info_label::compose(f, playback_info_label::Toggles(), false);
    ASSERT_FALSE(overlay.contains(QStringLiteral("->Scaled")));
    ASSERT_FALSE(twin.contains(QStringLiteral("->Scaled")));
    // the wiring: the accessor reads the texture-presentation key, never the GPU recon flag (the finding's tuple has it true)
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString unreduced = functionBody(source, QStringLiteral("int MainWindow::presentedPlaybackUnreducedCpuDebayerScale("));
    ASSERT_TRUE(unreduced.contains(QStringLiteral("telemetryBoolValue( telemetry, \"gpu_playback_recon_texture_present_active\" ),")));
    ASSERT_FALSE(unreduced.contains(QStringLiteral("\"gpu_playback_recon_used\"")));
    const QString header = readRepoFile(QStringLiteral("platform/qt/PlaybackInfoLabelPolicy.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("inline int unreducedCpuDebayerScale( bool processedStageMeasured, bool gpuTexturePresent, int sourceWidth,")));
}

// r16 (adversary pass 18 A1 on 0ec1f394): ordinary 1920x1088 AMaZE receipt, Don't Switch Debayer, GL viewport, GPU processing,
// GPU AMaZE debayer, MLVAPP_PLAYBACK_SCALE_FACTOR=2, recon texture presentation off. GPU AMaZE debayers and presents 1920 wide, but
// the helper refused a GPU-debayered frame and the shown scale fell back to the reported 2: the labels read "Half", not "Half->Full".
// GPU bilinear and requested 4/8 share it. (A2: an overflowing size input keeps the render's report.)
TEST(PlaybackInfoLabel, R16P18A1GpuDebayerFrameNamesTheScaleItWasShownAt)
{
    for (int scale : {2, 4, 8}) {
        const int shown = playback_info_label::unreducedCpuDebayerScale(false, false, 1920, 1920);
        ASSERT_EQ(1, shown);
        for (const QString gpu : {QStringLiteral("AMaZE"), QStringLiteral("Bilinear")}) {
            playback_info_label::Facts f = cpuFullScaleFacts(QStringLiteral("AMaZE"));
            f.gpuDisplay = true;
            f.debayerOnGpu = true;
            f.gpuDebayer = gpu;
            f.requestedScale = scale;
            f.presentedScale = shown;
            f.presentedValid = playback_info_label::presentedScaleBelongsToRequest(true, scale, f.requestedScale, f.presentedScale);
            f.debayerScale = shown;
            ASSERT_QSTR_EQ(QString(PlaybackScaleLabel::fractionWord(scale)) + QStringLiteral("->Full"), playback_info_label::scaleField(f));
            const QString expect = gpu == QStringLiteral("AMaZE") ? QStringLiteral("AMaZE (GPU)") : QStringLiteral("AMaZE->Bilinear (GPU)");
            ASSERT_QSTR_EQ(expect, playback_info_label::debayerField(f));
        }
    }
    // A2: sizes that would overflow the product keep the render's report
    ASSERT_EQ(2, playback_info_label::presentedScaleOfCpuPrepRoute(2, 2147483647, 1, 2));
    ASSERT_EQ(2, playback_info_label::presentedScaleOfCpuPrepRoute(2, 1920, 1920, 2147483647));
}

// r18 (keys B2 on 00c600a9, fable key3 + opus): CUDA texture route, playback scale 2 or 4 honoured, playing, a reduced GPU recon
// slot (1920 wide clip, 960 recon) whose texture no-readback skip is false (the AMaZE texture handoff unavailable, or a warm-up
// frame). The CPU route makes the frame full size and the render reports active 1, but render_thread_rendered_width kept the
// reduced 960 written with the slot, so unreducedCpuDebayerScale(false, false, 1920, 960) = 2: the label, the twin and the
// toolbar read "Half" / "Half res" and "<sel>->Scaled" over a full-size frame. The render thread now rewrites both rendered-size
// keys where it restores the full size (as the consumed-reduced-recon branch does); every reader then reads the frame's sizes.
TEST(PlaybackInfoLabel, R18B2ReducedCudaFrameMadeFullSizeByTheCpuRouteNamesFullAndTheSelectedDebayer)
{
    // the producer: the full-size restore rewrites both keys, after the slot fields and inside the same branch
    const QString render = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    const int restoreAt = render.indexOf(QStringLiteral("if( gpuReducedReconSlot && !skipCpuDebayerForGpuTextureNoReadback )"));
    ASSERT_TRUE(restoreAt >= 0);
    const int restoreEnd = render.indexOf(QStringLiteral("\n    }\n"), restoreAt);
    ASSERT_TRUE(restoreEnd > restoreAt);
    const QString restore = render.mid(restoreAt, restoreEnd - restoreAt);
    const int slotAt = restore.indexOf(QStringLiteral("slot.renderedImageWidth = renderedImageWidth;"));
    const int widthKeyAt = restore.indexOf(QStringLiteral("QStringLiteral(\"render_thread_rendered_width\"),\n            renderedImageWidth );"));
    const int heightKeyAt = restore.indexOf(QStringLiteral("QStringLiteral(\"render_thread_rendered_height\"),\n            renderedImageHeight );"));
    ASSERT_TRUE(slotAt >= 0);
    ASSERT_TRUE(widthKeyAt > slotAt);
    ASSERT_TRUE(heightKeyAt > slotAt);
    // the class sweep: every write of the rendered size after the first key insert is followed by its key rewrite
    ASSERT_EQ(3, render.count(QStringLiteral("QStringLiteral(\"render_thread_rendered_width\"),")));
    ASSERT_EQ(3, render.count(QStringLiteral("QStringLiteral(\"render_thread_rendered_height\"),")));

    // the frame's telemetry after the fix: no CPU processed stage, not texture-presented, 1920 of 1920, render active 1
    const int source = 1920;
    const int rendered = 1920;
    // the old stale tuple is what the finding read
    ASSERT_EQ(2, playback_info_label::unreducedCpuDebayerScale(false, false, source, 960));
    for (int scale : {2, 4}) {
        // reader 1: presentedPlaybackScaleShown off the CPU prep route (GPU preview prep) and presentedPlaybackDebayerScale /
        // presentedPlaybackUnreducedCpuDebayerScale, which both read unreducedCpuDebayerScale
        const int unreduced = playback_info_label::unreducedCpuDebayerScale(false, false, source, rendered);
        ASSERT_EQ(1, unreduced);
        // reader 2: presentedPlaybackScaleShown on the CPU prep route (the prep refuses: factor 1) and its debayer scale
        ASSERT_EQ(1, playback_info_label::presentedScaleOfCpuPrepRoute(1, source, rendered, 1));
        ASSERT_EQ(1, playback_info_label::debayerScaleOfCpuPrepRoute(source, rendered));
        for (bool gpuPrep : {true, false}) {
            const int shown = gpuPrep ? unreduced : playback_info_label::presentedScaleOfCpuPrepRoute(1, source, rendered, 1);
            const int debayerScale = gpuPrep ? unreduced : playback_info_label::debayerScaleOfCpuPrepRoute(source, rendered);
            // the label facts (MainWindow playbackInfoFacts and the relabel inputs read the same two accessors)
            playback_info_label::Facts f = cpuFullScaleFacts(QStringLiteral("AMaZE"));
            f.processingBadge = gpuPrep ? QStringLiteral("GPU Preview") : QStringLiteral("CPU");
            f.gpuDisplay = true;
            f.requestedScale = scale;
            f.presentedScale = shown;
            f.presentedValid = playback_info_label::presentedScaleBelongsToRequest(true, scale, f.requestedScale, f.presentedScale);
            f.debayerScale = debayerScale;
            ASSERT_QSTR_EQ(QString(PlaybackScaleLabel::fractionWord(scale)) + QStringLiteral("->Full"), playback_info_label::scaleField(f));
            ASSERT_QSTR_EQ(QStringLiteral("AMaZE"), playback_info_label::debayerField(f));
            const QString overlay = playback_info_label::compose(f, playback_info_label::Toggles(), true);
            const QString twin = playback_info_label::compose(f, playback_info_label::Toggles(), false);
            ASSERT_FALSE(overlay.contains(QStringLiteral("->Scaled")));
            ASSERT_FALSE(twin.contains(QStringLiteral("->Scaled")));
        }
    }
    // the readers: the toolbar cache key and label, the relabel inputs and the facts all read the two accessors, which read the
    // rewritten keys (so none of them can see the reduced width)
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(mw.contains(QStringLiteral("        presentedPlaybackScaleShown(),   // LOOK-LABEL-PLAYBACK-INFO-1 r12 (pass 14 A1)")));
    ASSERT_TRUE(mw.contains(QStringLiteral("const int activeScale = presentedPlaybackScaleShown();   // r12 pass-13 A1: the toolbar sibling")));
    ASSERT_TRUE(mw.contains(QStringLiteral("inputs.presentedScale = presentedPlaybackScaleShown();")));
    ASSERT_TRUE(mw.contains(QStringLiteral("inputs.debayerScale = presentedPlaybackDebayerScale();")));
    ASSERT_TRUE(mw.contains(QStringLiteral("facts.debayerScale = presentedPlaybackDebayerScale();")));
    const QString shownBody = functionBody(mw, QStringLiteral("int MainWindow::presentedPlaybackScaleShown("));
    ASSERT_TRUE(shownBody.contains(QStringLiteral("telemetryIntValue( telemetry, \"render_thread_rendered_width\" ),")));
    const QString unreducedBody = functionBody(mw, QStringLiteral("int MainWindow::presentedPlaybackUnreducedCpuDebayerScale("));
    ASSERT_TRUE(unreducedBody.contains(QStringLiteral("telemetryIntValue( telemetry, \"render_thread_rendered_width\" ) );")));
}

// r16 (adversary pass 17 A2 on 06faad35): HQ Dual-ISO, AMaZE, CPU 8-bit presentation, fast playback processing, scale 1, GPU
// recon on, texture presentation off. A GPU-reconstructed Full frame, then a Half preview frame (route 7) in the same Play: the
// CPU with-dimensions proxy recon kept the thread's "last recon used CUDA" flag, so the route guard refused route 7 and both
// surfaces read "AMaZE". The engine now clears the flag on every with-dimensions entry (pipeline test
// DualIsoPipeline.WithDimsCpuReconClearsTheLastGpuReconUsedFlag); the label then names the proxy for every proxy route.
TEST(PlaybackInfoLabel, R16P17A2ProxyAfterAGpuReconFrameNamesTheProxy)
{
    for (int tag : {6, 7, 9, 10}) {
        playback_info_label::PresentedRouteFacts r = cpuScale1RouteFacts(tag);
        r.gpuRecon = false;   // what the presented proxy frame's telemetry reads once the with-dimensions entry clears it
        playback_info_label::Facts f = cpuFullScaleFacts(QStringLiteral("AMaZE"));
        f.presentedRoute = playback_info_label::presentedRouteOfFrame(r);
        ASSERT_EQ(tag, f.presentedRoute);
        const QString level = (tag == 9 || tag == 10) ? QStringLiteral(" (Quarter proxy)") : QStringLiteral(" (Half proxy)");
        ASSERT_QSTR_EQ(QStringLiteral("AMaZE->Bilinear") + level, playback_info_label::debayerField(f));
    }
    // the engine side: the non-worker with-dimensions entry (every x1 proxy core and scaled pre-recon route calls it) clears
    // the GPU recon run state before its CPU recon, as the recon worker's entry already did
    const QString llrawproc = readRepoFile(QStringLiteral("src/mlv/llrawproc/llrawproc.c"));
    const QString entry = functionBody(llrawproc, QStringLiteral("int applyLLRawProcObject_with_dims(mlvObject_t * video,"));
    ASSERT_FALSE(entry.isEmpty());
    const int resetAt = entry.indexOf(QStringLiteral("llrawproc_gpu_playback_reset_last_run_state();"));
    ASSERT_TRUE(resetAt >= 0);
    ASSERT_TRUE(entry.indexOf(QStringLiteral("return llrawproc_apply_with_dims_internal(")) > resetAt);
}

// r20 (adversary pass 19 A1 on c6787183, introduced, major): 1920x1088 supported HQ Dual-ISO, AMaZE selected, Don't Switch Debayer,
// CUDA recon + GL, texture present on, MLVAPP_GPU_AMAZE_DEBAYER_DLL pointing at a missing file. The AMaZE R16 texture backend is
// unavailable, so the CPU AMaZE debayer is not skipped and the viewport presents the recon through the R16 bilinear shader
// (gl_r16_bilinear_shader). That branch, and the readback Bayer branch, never recorded the GPU bilinear debayer flag the label
// reads, so both surfaces said "AMaZE" over a frame the GPU debayered bilinear. Class: every presenter branch that hands the viewport
// a Bayer texture (the display shader debayers it) records usedGpuBilinearDebayer and clears the AMaZE flag.
TEST(PlaybackInfoLabel, R20A1BayerTexturePresenterBranchesRecordTheGpuBilinearDebayerTheLabelReads)
{
    // the label: the flags the frame carries decide the text
    playback_info_label::Facts stale = cpuFullScaleFacts(QStringLiteral("AMaZE"));
    stale.gpuDisplay = true;
    stale.debayerOnGpu = false;
    stale.gpuDebayer = QString();
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE"), playback_info_label::debayerField(stale));
    playback_info_label::Facts fixed = stale;
    fixed.debayerOnGpu = true;
    fixed.gpuDebayer = QStringLiteral("Bilinear");
    ASSERT_QSTR_EQ(QStringLiteral("AMaZE->Bilinear (GPU)"), playback_info_label::debayerField(fixed));
    // the producer: the presenter branches in MainWindow::presentPlaybackPreparedFrame
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source, QStringLiteral("void MainWindow::presentPlaybackPreparedFrame("));
    ASSERT_FALSE(body.isEmpty());
    const QString setBilinear = QStringLiteral("readyFrame.usedGpuBilinearDebayer = true;");
    const QString clearAmaze = QStringLiteral("readyFrame.usedGpuAmazeDebayer = false;");
    // branch 1: the no-readback R16 bilinear shader presenter
    const int shaderAt = body.indexOf(QStringLiteral("QStringLiteral(\"gl_r16_bilinear_shader\");"));
    ASSERT_TRUE(shaderAt >= 0);
    const int shaderBranchAt = body.lastIndexOf(QStringLiteral("if( framePresentedByViewport )"), shaderAt);
    ASSERT_TRUE(shaderBranchAt >= 0);
    const QString shaderBranch = body.mid(shaderBranchAt, shaderAt - shaderBranchAt);
    ASSERT_TRUE(shaderBranch.contains(setBilinear));
    ASSERT_TRUE(shaderBranch.contains(clearAmaze));
    // branch 2: the readback Bayer presenter
    const int bayerAt = body.indexOf(QStringLiteral("GpuDisplayViewport::presentBayer16("));
    ASSERT_TRUE(bayerAt >= 0);
    const int bayerEnd = body.indexOf(QStringLiteral("if( !gpuPlaybackReconTextureNoReadbackCandidate"), bayerAt);
    ASSERT_TRUE(bayerEnd > bayerAt);
    const QString bayerBranch = body.mid(bayerAt, bayerEnd - bayerAt);
    ASSERT_TRUE(bayerBranch.contains(setBilinear));
    ASSERT_TRUE(bayerBranch.contains(clearAmaze));
    // the class sweep: the Bayer-texture presenters are exactly these two calls, and each records the flag
    ASSERT_EQ(1, body.count(QStringLiteral("GpuDisplayViewport::presentGpuPlaybackReconTexture(")));
    ASSERT_EQ(1, body.count(QStringLiteral("GpuDisplayViewport::presentBayer16(")));
    // r21 (Grok G1 residual): the global count of the flag writes is replaced by the per-branch flag-pair assertion below
    // (R21G1EveryPresenterBranchCarriesExactlyOneOfTheTwoGpuDebayerFlags): readyFrame can arrive carrying the render thread's
    // bilinear flag, so every AMaZE write clears it as every bilinear write clears the AMaZE flag.
    ASSERT_EQ(2, body.count(setBilinear));
}

namespace
{

// r21: the innermost brace block that holds `pos` (the presenter branch a flag write belongs to), braces counted in source order.
QString enclosingBlock(const QString &text, int pos)
{
    int depth = 0;
    int start = 0;
    for ( int i = pos - 1; i >= 0; --i )
    {
        if ( text.at(i) == QLatin1Char('}') ) ++depth;
        else if ( text.at(i) == QLatin1Char('{') )
        {
            if ( depth == 0 ) { start = i; break; }
            --depth;
        }
    }
    depth = 0;
    int end = text.size();
    for ( int i = pos; i < text.size(); ++i )
    {
        if ( text.at(i) == QLatin1Char('{') ) ++depth;
        else if ( text.at(i) == QLatin1Char('}') )
        {
            if ( depth == 0 ) { end = i; break; }
            --depth;
        }
    }
    return text.mid(start, end - start);
}

QList<int> allIndexesOf(const QString &text, const QString &needle)
{
    QList<int> at;
    for ( int i = text.indexOf(needle); i >= 0; i = text.indexOf(needle, i + 1) ) at.append(i);
    return at;
}

} // namespace

// r21 (Grok G1 residual on #834, comment 7061902251): readyFrame is acquired from the render thread (acquireLatestReadyFrame copies
// the slot's flags, RenderFrameThread.cpp:1896-1899) and the slot can carry usedGpuBilinearDebayer=true (RenderFrameThread.cpp:5423).
// An AMaZE presenter that sets usedGpuAmazeDebayer without clearing the bilinear flag presents a frame that carries both, and the
// label's gpuDebayer reads AMaZE first but debayerOnGpu/route read either. The sweep is per branch: the branch that sets one flag true
// sets the other false, and no presented branch carries both true.
TEST(PlaybackInfoLabel, R21G1EveryPresenterBranchCarriesExactlyOneOfTheTwoGpuDebayerFlags)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source, QStringLiteral("void MainWindow::presentPlaybackPreparedFrame("));
    ASSERT_FALSE(body.isEmpty());
    const QString amazeTrue = QStringLiteral("readyFrame.usedGpuAmazeDebayer = true;");
    const QString amazePresented = QStringLiteral("readyFrame.usedGpuAmazeDebayer = framePresentedByViewport;");
    const QString bilinearTrue = QStringLiteral("readyFrame.usedGpuBilinearDebayer = true;");
    const QString amazeFalse = QStringLiteral("readyFrame.usedGpuAmazeDebayer = false;");
    const QString bilinearFalse = QStringLiteral("readyFrame.usedGpuBilinearDebayer = false;");
    // the writers: four AMaZE presenters (post-WB texture, the direct CUDA AMaZE post-WB texture handoff (INFO-2 r4, adversary r3
    // pass-1 A10), RGB16 offscreen fallback, CUDA AMaZE readback fallback), two bilinear
    const QList<int> amazeSites = allIndexesOf(body, amazeTrue) + allIndexesOf(body, amazePresented);
    const QList<int> bilinearSites = allIndexesOf(body, bilinearTrue);
    ASSERT_EQ(4, amazeSites.size());
    // A10: the direct CUDA AMaZE texture handoff branch (the one that names cuda_gl_rgba16_amaze_texture) writes the flag pair itself
    const int cudaAmazeAt = body.indexOf(QStringLiteral("QStringLiteral(\"cuda_gl_rgba16_amaze_texture\")"));
    ASSERT_TRUE(cudaAmazeAt >= 0);
    const QString cudaAmazeBranch = enclosingBlock(body, cudaAmazeAt);
    ASSERT_TRUE(cudaAmazeBranch.contains(amazeTrue));
    ASSERT_TRUE(cudaAmazeBranch.contains(bilinearFalse));
    ASSERT_EQ(2, bilinearSites.size());
    for ( int at : amazeSites )
    {
        const QString branch = enclosingBlock(body, at);
        ASSERT_TRUE(branch.contains(bilinearFalse));   // the AMaZE branch clears the bilinear flag
        ASSERT_FALSE(branch.contains(bilinearTrue));   // and no presented branch carries both
    }
    for ( int at : bilinearSites )
    {
        const QString branch = enclosingBlock(body, at);
        ASSERT_TRUE(branch.contains(amazeFalse));      // the bilinear branch clears the AMaZE flag
        ASSERT_FALSE(branch.contains(amazeTrue));
        ASSERT_FALSE(branch.contains(amazePresented));
    }
}

// r21 (Grok G4): the GPU bilinear renderer description is read at recordPresentedFrame (m_lastPresentedGpuBilinearRendererDescription).
// The two presenter branches that set usedGpuBilinearDebayer never set it, so it read empty for a frame the label calls GPU Bilinear.
TEST(PlaybackInfoLabel, R21G4TheBayerPresenterBranchesSetTheGpuBilinearRendererDescription)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source, QStringLiteral("void MainWindow::presentPlaybackPreparedFrame("));
    ASSERT_FALSE(body.isEmpty());
    const QString bilinearTrue = QStringLiteral("readyFrame.usedGpuBilinearDebayer = true;");
    const QString description = QStringLiteral("readyFrame.gpuBilinearRendererDescription =");
    const QList<int> sites = allIndexesOf(body, bilinearTrue);
    ASSERT_EQ(2, sites.size());
    for ( int at : sites ) ASSERT_TRUE(enclosingBlock(body, at).contains(description));
    // branch 1 names the handoff mode it just set (gl_r16_bilinear_shader)
    const int shaderAt = body.indexOf(QStringLiteral("QStringLiteral(\"gl_r16_bilinear_shader\");"));
    ASSERT_TRUE(shaderAt >= 0);
    const int descriptionAt = body.indexOf(QStringLiteral("readyFrame.gpuBilinearRendererDescription = texturePresentHandoffMode;"), shaderAt);
    ASSERT_TRUE(descriptionAt > shaderAt);
    // the one reader
    const QString recorded = functionBody(source, QStringLiteral("void MainWindow::recordPresentedFrame("));
    ASSERT_TRUE(recorded.contains(QStringLiteral("m_lastPresentedGpuBilinearRendererDescription = readyFrame.gpuBilinearRendererDescription;")));
    // the docs line: the flag is not always false; the render-thread GPU bilinear path sets it with a renderer
    const QString docs = readRepoFile(QStringLiteral("docs/13-testing-infrastructure.md"));
    ASSERT_FALSE(docs.contains(QStringLiteral("- `gpu_bilinear_debayer_active` stays `false`.")));
    ASSERT_TRUE(docs.contains(QStringLiteral("`gpu_bilinear_debayer_active` is `false` on a software renderer only because the probe reports unavailable")));
}

// r21 (Grok G2): togglesFromSettings reads the six Show* keys, each default true, from the app's settings store; MainWindow's
// settings load and the Edit-settings reload both call it. An isolated INI store (automation_settings) keeps the test off the registry.
TEST(PlaybackInfoLabel, R21G2TogglesFromSettingsReadsTheSixKeysWithDefaultTrueAndTheLoadPathCallsIt)
{
    const QString dir = QDir::tempPath() + QStringLiteral("/mlvapp-r21g2-") + QUuid::createUuid().toString(QUuid::WithoutBraces);
    ASSERT_FALSE(automation_settings::isolate(dir).isEmpty());
    // an empty store: every field defaults on
    playback_info_label::Toggles defaults = playback_info_label::togglesFromSettings();
    ASSERT_TRUE(defaults.pipeline && defaults.scale && defaults.debayer && defaults.fps && defaults.dualIso && defaults.resolution);
    ASSERT_TRUE(playback_info_label::Settings::kDefaultShow());
    ASSERT_QSTR_EQ(QStringLiteral("Playback/LookLabelInfo/ShowPipeline"), QString::fromLatin1(playback_info_label::Settings::kKeyShowPipeline()));
    ASSERT_QSTR_EQ(QStringLiteral("Playback/LookLabelInfo/ShowResolution"), QString::fromLatin1(playback_info_label::Settings::kKeyShowResolution()));
    // each key switches only its own field off
    const char *keys[6] = { playback_info_label::Settings::kKeyShowPipeline(), playback_info_label::Settings::kKeyShowScale(),
                            playback_info_label::Settings::kKeyShowDebayer(), playback_info_label::Settings::kKeyShowFps(),
                            playback_info_label::Settings::kKeyShowDualIso(), playback_info_label::Settings::kKeyShowResolution() };
    for ( int k = 0; k < 6; ++k )
    {
        {
            auto store = automation_settings::openAppSettings();
            store->clear();
            store->setValue(QString::fromLatin1(keys[k]), false);
            store->sync();
        }
        const playback_info_label::Toggles t = playback_info_label::togglesFromSettings();
        const bool fields[6] = { t.pipeline, t.scale, t.debayer, t.fps, t.dualIso, t.resolution };
        for ( int f = 0; f < 6; ++f ) ASSERT_EQ(f != k, fields[f]);
    }
    // the write helper round-trips through the same keys (INFO-2 r2 A6: one toggle at a time, each its own key)
    playback_info_label::Toggles written = togglesFromMask(0x2A);   // scale, fps, resolution on
    {
        auto store = automation_settings::openAppSettings();
        store->clear();
        store->sync();
    }
    playback_info_label::toggleWriteToSettings(&playback_info_label::Toggles::pipeline, written.pipeline);
    playback_info_label::toggleWriteToSettings(&playback_info_label::Toggles::scale, written.scale);
    playback_info_label::toggleWriteToSettings(&playback_info_label::Toggles::debayer, written.debayer);
    playback_info_label::toggleWriteToSettings(&playback_info_label::Toggles::fps, written.fps);
    playback_info_label::toggleWriteToSettings(&playback_info_label::Toggles::dualIso, written.dualIso);
    playback_info_label::toggleWriteToSettings(&playback_info_label::Toggles::resolution, written.resolution);
    const playback_info_label::Toggles read = playback_info_label::togglesFromSettings();
    ASSERT_TRUE(!read.pipeline && read.scale && !read.debayer && read.fps && !read.dualIso && read.resolution);
    automation_settings::releaseForTest();
    QDir(dir).removeRecursively();
    // the load path: the two MainWindow settings reads call it
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_EQ(2, source.count(QStringLiteral("m_playbackInfoToggles = playback_info_label::togglesFromSettings();")));
}

// r21 (adversary pass 20 A1, introduced, major): MLVAPP_EXPERIMENTAL_GL_VIEWPORT=1, a clip presented by the GPU widget viewport,
// fullscreen look label and Scale enabled. GpuDisplayViewport::eventFilter consumes the Paint event while a frame is pending, so
// QGraphicsView::paintEvent never runs and GraphicsZoomView::drawForeground (the only caller of the label blit) never draws the
// look label or the playback-info lines. The viewport's own paintGL now hands the label to the view's one drawing routine.
TEST(PlaybackInfoLabel, R21A1TheGpuWidgetViewportPaintDrawsTheLookLabelAndThePlaybackInfo)
{
    const QString viewport = readRepoFile(QStringLiteral("platform/qt/GpuDisplayViewport.cpp"));
    const QString paintGl = functionBody(viewport, QStringLiteral("void GpuDisplayViewport::paintGL()"));
    ASSERT_FALSE(paintGl.isEmpty());
    // the consumed Paint event runs paintGL (QOpenGLWidget::event), which draws the frame and then the label over it
    // (called by name: GpuDisplayViewport.cpp is also built into gui_tests, which does not link GraphicsZoomView)
    ASSERT_TRUE(paintGl.contains(QStringLiteral("QMetaObject::invokeMethod(m_view, \"paintLookLabel\", Qt::DirectConnection")));
    const int frameDrawAt = paintGl.indexOf(QStringLiteral("glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);"));
    ASSERT_TRUE(frameDrawAt >= 0);
    ASSERT_TRUE(paintGl.indexOf(QStringLiteral("\"paintLookLabel\"")) > frameDrawAt);   // over the frame, never under it
    const QString zoomHeader = readRepoFile(QStringLiteral("platform/qt/GraphicsZoomView.h"));
    ASSERT_TRUE(zoomHeader.contains(QStringLiteral("Q_INVOKABLE void paintLookLabel(QPainter *painter);")));
    const QString gui = readRepoFile(QStringLiteral("tests/gui/gui_tests.pro"));
    ASSERT_FALSE(gui.contains(QStringLiteral("GraphicsZoomView.cpp")));   // so a direct call from the viewport would not link there
    // the view's CPU paint (drawForeground) and the GPU viewport's paint draw through the same routine, so the receipt's drawn
    // text (lookLabelDrawnText) is whatever either surface last drew
    const QString zoom = readRepoFile(QStringLiteral("platform/qt/GraphicsZoomView.cpp"));
    const QString foreground = functionBody(zoom, QStringLiteral("void GraphicsZoomView::drawForeground("));
    ASSERT_TRUE(foreground.contains(QStringLiteral("paintLookLabel( painter );")));
    const QString label = functionBody(zoom, QStringLiteral("void GraphicsZoomView::paintLookLabel("));
    ASSERT_TRUE(label.contains(QStringLiteral("m_lookLabelDrawnText = m_lookLabelText;")));
    ASSERT_TRUE(label.contains(QStringLiteral("painter->drawPixmap(")));
}

// r21 (adversary pass 20 A2, introduced, major): a paused full-size CPU frame made with LMMSE, the debayer field on; AMaZE is selected
// (m_pChosenDebayer text changes at once) and the label paints while the asynchronous replacement is pending: it read "AMaZE" over
// the LMMSE frame. The debayer name is recorded on the request context when the frame is requested, and the field reads the
// PRESENTED frame's context.
TEST(PlaybackInfoLabel, R21A2TheDebayerFieldReadsTheDebayerThatProducedThePresentedFrame)
{
    // the label: the field is exactly the facts' name (nothing else substitutes the current selection)
    playback_info_label::Facts presented = cpuFullScaleFacts(QStringLiteral("LMMSE"));
    ASSERT_QSTR_EQ(QStringLiteral("LMMSE"), playback_info_label::debayerField(presented));
    const QString header = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("QString debayerName;")));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    // written when the frame is requested (drawFrame), read from the presented frame's context (playbackInfoFacts)
    const QString draw = functionBody(source, QStringLiteral("void MainWindow::drawFrame( bool updateTimecodeLabel )"));
    ASSERT_TRUE(draw.contains(QStringLiteral("requestContext.debayerName = m_pChosenDebayer ? m_pChosenDebayer->text() : QString();")));
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts( void )"));
    ASSERT_FALSE(facts.contains(QStringLiteral("facts.debayer = m_pChosenDebayer ? m_pChosenDebayer->text() : QString();")));
    // INFO-2 r1 (pass 21 A1): the facts read it through playbackInfoDebayerName, which reads the presented frame's context
    ASSERT_TRUE(facts.contains(QStringLiteral("facts.debayer = playbackInfoDebayerName();")));
    const QString reader = functionBody(source, QStringLiteral("QString MainWindow::playbackInfoDebayerName( void ) const"));
    ASSERT_TRUE(reader.contains(QStringLiteral("m_lastPresentedRequestContext.debayerName")));
    // the class: no label reader takes the debayer text from the live selection
    const QString info = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackInfoLabel("));
    ASSERT_FALSE(info.contains(QStringLiteral("m_pChosenDebayer")));
}

// r21 (adversary pass 20 A3, introduced, major): 24 fps Play; the first request at t=0 reaches present_nothing (no successful
// presenter, no prepared image); the second at t=300 ms has a nonzero tick interval and supplies the first paint after the relabel
// gate opens. The meter timestamped both requests and published 3.3/24 fps with no interval between presented frames. Only a
// presented frame advances the meter's timestamp (recordPresentedFrame counts them).
TEST(PlaybackInfoLabel, R21A3OnlyAPresentedFrameAdvancesTheFpsMeter)
{
    // the pure step: a request that presented nothing feeds no interval and does not re-arm the draw timestamp
    ASSERT_TRUE(playback_fps_meter::drawFeedsMeter(true));
    ASSERT_FALSE(playback_fps_meter::drawFeedsMeter(false));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString record = functionBody(source, QStringLiteral("void MainWindow::recordPresentedFrame("));
    ASSERT_TRUE(record.contains(QStringLiteral("++m_playbackPresentedFrameCount;")));
    const QString tick = functionBody(source, QStringLiteral("void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )"));
    const int gateAt = tick.indexOf(QStringLiteral("playback_fps_meter::drawFeedsMeter("));
    ASSERT_TRUE(gateAt >= 0);
    // the Play's first-draw flag, the timestamp, the interval and the smoothing all sit behind the presented-frame check
    ASSERT_TRUE(tick.indexOf(QStringLiteral("const bool firstDrawOfPlay = m_playbackFpsMeterNewPlay;")) > gateAt);
    ASSERT_TRUE(tick.indexOf(QStringLiteral("lastDrawTime = nowTime;")) > gateAt);
    ASSERT_TRUE(tick.indexOf(QStringLiteral("playback_fps_meter::meterIntervalMs(")) > gateAt);
    ASSERT_TRUE(tick.indexOf(QStringLiteral("playback_fps_meter::smoothedFrameMs(")) > gateAt);
    ASSERT_TRUE(tick.contains(QStringLiteral("m_playbackFpsMeterSeenPresentedCount = m_playbackPresentedFrameCount;")));
}

// r21 (adversary pass 20 A4, introduced, major): 24 fps normal Play, EMA 40 ms, last draw and update 23:59:59.980; the first
// post-midnight tick at 00:00:00.022 has whole-frame credit and draws. The draw branch handed -86399958 to the smoothing, which
// PRESERVED 40 ms (a negative age is stalled everywhere else), replaced the timestamp and republished 25.0/24 fps. A negative age
// clears the previous measured rate and the corner reads "--" until an interval inside the new timeline exists.
TEST(PlaybackInfoLabel, R21A4ANegativeDrawAgeClearsThePreviousMeasuredRate)
{
    const int midnightWrapMs = -86399958;   // QTime(23,59,59,980).msecsTo(QTime(0,0,0,22))
    ASSERT_TRUE(playback_fps_meter::fpsMeterStalled(midnightWrapMs));
    ASSERT_TRUE(playback_fps_meter::smoothedFrameMs(40.0, midnightWrapMs) <= 0.0);   // cleared, never the preserved 40 ms
    ASSERT_TRUE(playback_fps_meter::smoothedFrameMs(40.0, -1) <= 0.0);
    // zero (same millisecond) still leaves the average alone, and the next positive interval reseeds from itself
    ASSERT_EQ(40.0, playback_fps_meter::smoothedFrameMs(40.0, 0));
    ASSERT_EQ(41.0, playback_fps_meter::smoothedFrameMs(playback_fps_meter::smoothedFrameMs(40.0, midnightWrapMs), 41));
    // the wiring: a cleared average withdraws the shown rate (the rate is not republished from the preserved average)
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString tick = functionBody(source, QStringLiteral("void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )"));
    const int clearAt = tick.indexOf(QStringLiteral("if( measuredFrameMs < 0 )"));
    ASSERT_TRUE(clearAt >= 0);
    // the one reset (it zeroes the status text, the average and the shown rate, and withdraws the corner's rate), reached because
    // fpsMeterShouldReset reads a negative age as stalled (R7A1ANegativeOrWrappedDrawAgeCountsAsStalled)
    // INFO-2 r2 (pass 1 A2): the interval reads presentation stamps, so a wrapped stamp age forces the reset itself (the tick clock's own age
    // can be positive while two stamps straddle the wrap)
    ASSERT_TRUE(tick.mid(clearAt, 300).contains(QStringLiteral("resetFpsMeterIfIdle( nowTime, true );")));
    ASSERT_TRUE(playback_fps_meter::fpsMeterShouldReset(true, true, midnightWrapMs));
}

// INFO-2 r1 (adversary pass 21 A1 on deebf5bb, introduced, major): pause on a full-size CPU LMMSE frame, select AMaZE, and the
// replacement frame arrives on the same pipeline, scale, route and GPU flags. The relabel key (PlaybackInfoInputs) lacked the debayer
// name, so refreshPlaybackInfoOnInputChange compared equal and the label kept "LMMSE". The key carries the shown name, read by the
// one reader the label's facts use.
TEST(PlaybackInfoLabel, InfoTwoR1A1TheRelabelKeyCarriesTheDebayerNameTheLabelShows)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const int structAt = header.indexOf(QStringLiteral("struct PlaybackInfoInputs"));
    ASSERT_TRUE(structAt >= 0);
    const int structEnd = header.indexOf(QStringLiteral("PlaybackInfoInputs m_playbackInfoInputs;"), structAt);
    ASSERT_TRUE(structEnd > structAt);
    const QString key = header.mid(structAt, structEnd - structAt);
    ASSERT_TRUE(key.contains(QStringLiteral("QString debayer;")));
    ASSERT_TRUE(key.contains(QStringLiteral("debayer == o.debayer")));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString refresh = functionBody(source, QStringLiteral("void MainWindow::refreshPlaybackInfoOnInputChange("));
    ASSERT_FALSE(refresh.isEmpty());
    ASSERT_TRUE(refresh.contains(QStringLiteral("inputs.debayer = playbackInfoDebayerName();")));
    ASSERT_TRUE(refresh.indexOf(QStringLiteral("inputs.debayer = playbackInfoDebayerName();")) < refresh.indexOf(QStringLiteral("if( inputs == m_playbackInfoInputs ) return;")));
    // the class: the label's facts and the key read the name through the same reader (no second copy of the rule)
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts( void )"));
    ASSERT_TRUE(facts.contains(QStringLiteral("facts.debayer = playbackInfoDebayerName();")));
    const QString reader = functionBody(source, QStringLiteral("QString MainWindow::playbackInfoDebayerName( void ) const"));
    ASSERT_TRUE(reader.contains(QStringLiteral("m_lastPresentedRequestContext.debayerName")));
    ASSERT_FALSE(facts.contains(QStringLiteral("m_lastPresentedRequestContext.debayerName")));   // R21A2's pin moved into the reader
    // the effect on the text: the two names the key now tells apart compose to different labels
    playback_info_label::Facts before = cpuFullScaleFacts(QStringLiteral("LMMSE"));
    playback_info_label::Facts after = cpuFullScaleFacts(QStringLiteral("AMaZE"));
    ASSERT_TRUE(playback_info_label::compose(before, playback_info_label::Toggles(), true)
                != playback_info_label::compose(after, playback_info_label::Toggles(), true));
}

// INFO-2 r1 (adversary pass 21 A5 on deebf5bb, introduced, major): paused 1920x1080 CPU processed-8 frame, LMMSE, scale 1. Select
// scale 4 and read the label before the replacement frame is presented: the live request (4) no longer matches the frame's request
// context, presentedValid went false, and the debayer field took the LIVE scale ("LMMSE->Scaled") over a frame debayered full size.
// The field reads the presented frame's own scale (presentedFrameScale).
TEST(PlaybackInfoLabel, InfoTwoR1A5TheDebayerAttributionReadsThePresentedFramesScaleNotTheLiveRequest)
{
    ASSERT_EQ(1, playback_info_label::presentedFrameScaleOf(true, 1));
    ASSERT_EQ(8, playback_info_label::presentedFrameScaleOf(true, 8));
    ASSERT_EQ(0, playback_info_label::presentedFrameScaleOf(false, 1));   // no presented request context
    ASSERT_EQ(0, playback_info_label::presentedFrameScaleOf(true, 3));
    ASSERT_EQ(0, playback_info_label::presentedFrameScaleOf(true, 0));
    ASSERT_EQ(0, playback_info_label::presentedFrameScaleOf(true, -1));
    // the paused scale change: the frame on screen is Full, the live request is Quarter
    playback_info_label::Facts f = cpuFullScaleFacts(QStringLiteral("LMMSE"));
    f.requestedScale = 4;
    f.presentedScale = 1;
    f.presentedValid = playback_info_label::presentedScaleBelongsToRequest(true, 1, f.requestedScale, f.presentedScale);
    ASSERT_FALSE(f.presentedValid);   // the scale field keeps the toolbar's rule (the asked scale until the frame follows)
    f.presentedFrameScale = playback_info_label::presentedFrameScaleOf(true, f.presentedScale);
    ASSERT_QSTR_EQ(QStringLiteral("LMMSE"), playback_info_label::debayerField(f));   // the retained frame was debayered full size
    // the other direction: the retained frame is Quarter while the live request went back to Full
    playback_info_label::Facts g = cpuFullScaleFacts(QStringLiteral("LMMSE"));
    g.requestedScale = 1;
    g.presentedScale = 4;
    g.presentedValid = false;
    g.presentedFrameScale = playback_info_label::presentedFrameScaleOf(true, 4);
    ASSERT_QSTR_EQ(QStringLiteral("LMMSE->Scaled"), playback_info_label::debayerField(g));
    // a frame that says its own debayer scale (a playback-prep frame) still decides first
    g.debayerScale = 1;
    ASSERT_QSTR_EQ(QStringLiteral("LMMSE"), playback_info_label::debayerField(g));
    // no presented frame: the legacy fallback (the requested scale) is unchanged
    playback_info_label::Facts none = cpuFullScaleFacts(QStringLiteral("LMMSE"));
    none.requestedScale = 4;
    none.presentedValid = false;
    none.presentedScale = 0;
    ASSERT_QSTR_EQ(QStringLiteral("LMMSE->Scaled"), playback_info_label::debayerField(none));
    // the wiring: playbackInfoFacts hands the presented frame's scale to the label
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts( void )"));
    ASSERT_TRUE(facts.contains(QStringLiteral("facts.presentedFrameScale = playback_info_label::presentedFrameScaleOf( m_lastPresentedRequestContextValid, activeScale );")));
}

// INFO-2 r2 (adversary pass 1 A2 on b056b902, introduced, major): a 24 fps clip, successful handoffs at 8 and 117 ms, observing
// request ticks at 42 and 126 ms. The meter stamped the TICKS, so it read 126 - 42 = 84 ms = 11.9/24 instead of the 109 ms between
// the two presentations = 9.2/24. The interval is measured on the stamps recordPresentedFrame takes at the handoff (QTime stays: the
// clock type belongs to PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1). Frames presented between two steps share the span.
TEST(PlaybackInfoLabel, InfoTwoR2A2TheMeterIntervalIsBetweenPresentationsNotBetweenTheObservingTicks)
{
    const int handoffA = 8, handoffB = 117, tickA = 42, tickB = 126;
    const int between = playback_fps_meter::presentedIntervalMs(handoffB - handoffA, 1);
    ASSERT_EQ(109, between);
    ASSERT_QSTR_EQ(QStringLiteral("9.2"), QString::number(1000.0 / playback_fps_meter::smoothedFrameMs(0.0, between), 'f', 1));
    ASSERT_QSTR_EQ(QStringLiteral("11.9"), QString::number(1000.0 / (tickB - tickA), 'f', 1));   // what the tick stamps said
    // two handoffs inside one observed span are two frames of it, never one long frame; a non-positive span passes through
    ASSERT_EQ(42, playback_fps_meter::presentedIntervalMs(84, 2));
    ASSERT_EQ(33, playback_fps_meter::presentedIntervalMs(100, 3));
    ASSERT_EQ(109, playback_fps_meter::presentedIntervalMs(109, 0));
    ASSERT_EQ(0, playback_fps_meter::presentedIntervalMs(0, 4));
    ASSERT_EQ(-86399958, playback_fps_meter::presentedIntervalMs(-86399958, 2));   // a wrap stays negative: stalled
    // the wiring: the handoff takes the stamp, the meter step reads stamps, and the tick clock (QTime) is not swapped for another type
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString record = functionBody(source, QStringLiteral("void MainWindow::recordPresentedFrame("));
    ASSERT_TRUE(record.contains(QStringLiteral("m_playbackFpsMeterPresentedStamp = QTime::currentTime();")));
    const QString tick = functionBody(source, QStringLiteral("void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )"));
    ASSERT_TRUE(tick.contains(QStringLiteral("const QTime presentedStamp = m_playbackFpsMeterPresentedStamp;")));
    ASSERT_TRUE(tick.contains(QStringLiteral("m_playbackFpsMeterPrevPresentedStamp = presentedStamp;")));
    ASSERT_TRUE(tick.contains(QStringLiteral("m_playbackFpsMeterPrevPresentedStamp = QTime();")));   // a restart forgets it
    ASSERT_FALSE(tick.contains(QStringLiteral("QElapsedTimer")));
    ASSERT_TRUE(tick.contains(QStringLiteral("static QTime lastDrawTime;")));
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("QTime m_playbackFpsMeterPresentedStamp;")));
    ASSERT_TRUE(header.contains(QStringLiteral("QTime m_playbackFpsMeterPrevPresentedStamp;")));
    // the frames since the last step divide the span (the count the r21 gate already keeps)
    ASSERT_TRUE(tick.contains(QStringLiteral(
        "qMin<quint64>( m_playbackPresentedFrameCount - m_playbackFpsMeterSeenPresentedCount, 1000000 ) );")));
}

// INFO-2 r2 (adversary pass 1 A2, POLICY_MUTANT_SURVIVED on b056b902 platform/qt/MainWindow.cpp:3135): the whole presented-since-step
// statement and its gate, so `!presentedSinceMeterStep` (a meter that steps only on a request that presented NOTHING) fails here.
TEST(PlaybackInfoLabel, InfoTwoR2PolicyCallSitePinTheMeterGateKeepsItsFullArgument)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "const bool presentedSinceMeterStep = m_playbackPresentedFrameCount != m_playbackFpsMeterSeenPresentedCount;\n"
        "        if( playback_fps_meter::drawFeedsMeter( presentedSinceMeterStep ) )")));
}

// INFO-2 r2 (POLICY_MUTANT_SURVIVED on b056b902 platform/qt/MainWindow.cpp:34428 and :34429): the two telemetry flags of
// presentedPlaybackUnreducedCpuDebayerScale, pinned with the whole argument list. An inverted flag reads a frame the CPU debayered at
// scale 1 as a GPU-recon frame (and the other way round), so the label names the wrong scale.
TEST(PlaybackInfoLabel, InfoTwoR2PolicyCallSitePinUnreducedCpuDebayerScaleKeepsItsFullArgumentList)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "return playback_info_label::unreducedCpuDebayerScale(\n"
        "        telemetryBoolValue( telemetry, \"processed16_stage_measured\" ),\n"
        "        telemetryBoolValue( telemetry, \"gpu_playback_recon_texture_present_active\" ),")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "        telemetryIntValue( telemetry, \"render_thread_source_width\" ),\n"
        "        telemetryIntValue( telemetry, \"render_thread_rendered_width\" ) );")));
}

// INFO-2 r2 (adversary pass 1 A3 on b056b902, introduced, major): HQ Dual-ISO, GPU recon and texture present enabled, AMaZE selected,
// the AMaZE DLL missing, a successful R16 bilinear present. r20 made the presenter record gpu_bilinear_debayer_active = true (and the
// AMaZE sibling), and the headless sample's worker-telemetry loop then overwrote the key with the worker's own false next to the
// shader description. The loop skips both keys.
TEST(PlaybackInfoLabel, InfoTwoR2A3TheHeadlessSampleKeepsThePresentersGpuDebayerFlags)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int recordedAt = source.indexOf(QStringLiteral("sample.insert( QStringLiteral(\"gpu_bilinear_debayer_active\"),\n"
                                                         "                           m_lastPresentedFrameUsedGpuBilinearDebayer );"));
    ASSERT_TRUE(recordedAt >= 0);
    const int amazeAt = source.indexOf(QStringLiteral("sample.insert( QStringLiteral(\"gpu_amaze_debayer_active\"),\n"
                                                      "                           m_lastPresentedFrameUsedGpuAmazeDebayer );"), recordedAt);
    ASSERT_TRUE(amazeAt > recordedAt);
    const int loopAt = source.indexOf(QStringLiteral("for( auto it = stageTimingTelemetry.constBegin();"), amazeAt);
    ASSERT_TRUE(loopAt > amazeAt);
    const int loopEnd = source.indexOf(QStringLiteral("const QJsonObject frameColorTelemetry"), loopAt);
    ASSERT_TRUE(loopEnd > loopAt);
    const QString loop = source.mid(loopAt, loopEnd - loopAt);
    ASSERT_TRUE(loop.contains(QStringLiteral(
        "if( it.key() == QStringLiteral(\"gpu_bilinear_debayer_active\")\n"
        "                     || it.key() == QStringLiteral(\"gpu_amaze_debayer_active\") ) continue;")));
    ASSERT_TRUE(loop.indexOf(QStringLiteral("continue;")) < loop.indexOf(QStringLiteral("sample.insert( it.key(), it.value() );")));
}

// INFO-2 r2 (adversary pass 1 A5 on b056b902, introduced, major): pause on a valid HQ Dual-ISO frame with Raw Corrections on, then
// uncheck Raw Corrections before its replacement frame lands. The field read the live settings, so the label said "Dual-ISO off" over
// the already reconstructed frame. The field reads the presented frame's own state, recorded when it was requested, and the relabel
// key carries it so the replacement frame relabels.
TEST(PlaybackInfoLabel, InfoTwoR2A5TheDualIsoFieldDescribesTheDisplayedImageNotTheLiveSettings)
{
    // the label: the field is exactly the facts' state
    playback_info_label::Facts shown = ownerFacts();
    shown.dualIsoClip = true;
    shown.dualIsoOn = true;
    ASSERT_QSTR_EQ(QStringLiteral("Dual-ISO on"), playback_info_label::dualIsoField(shown));
    const QString header = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("bool dualIsoOn = false;")));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString draw = functionBody(source, QStringLiteral("void MainWindow::drawFrame( bool updateTimecodeLabel )"));
    ASSERT_TRUE(draw.contains(QStringLiteral("requestContext.dualIsoOn = playbackInfoLiveDualIsoOn();")));
    const QString facts = functionBody(source, QStringLiteral("playback_info_label::Facts MainWindow::playbackInfoFacts( void )"));
    ASSERT_TRUE(facts.contains(QStringLiteral("facts.dualIsoOn = playbackInfoDualIsoOn();")));
    ASSERT_FALSE(facts.contains(QStringLiteral("llrpGetFixRawMode")));   // no live read in the facts
    const QString reader = functionBody(source, QStringLiteral("bool MainWindow::playbackInfoDualIsoOn( void ) const"));
    ASSERT_TRUE(reader.contains(QStringLiteral(
        "return m_lastPresentedRequestContextValid ? m_lastPresentedRequestContext.dualIsoOn : playbackInfoLiveDualIsoOn();")));
    const QString key = functionBody(source, QStringLiteral("void MainWindow::refreshPlaybackInfoOnInputChange("));
    ASSERT_TRUE(key.contains(QStringLiteral("inputs.dualIsoOn = playbackInfoDualIsoOn();")));
    const QString mainHeader = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(mainHeader.contains(QStringLiteral("&& dualIsoOn == o.dualIsoOn")));
    // the class: no other label reader takes the Dual-ISO state from the live settings
    const QString info = functionBody(source, QStringLiteral("void MainWindow::updatePlaybackInfoLabel("));
    ASSERT_FALSE(info.contains(QStringLiteral("playbackInfoLiveDualIsoOn")));
}

// INFO-2 r2 (adversary pass 1 A6 on b056b902, introduced, major): two interactive instances load all toggles enabled; A saves FPS off,
// then B saves Dual-ISO off from its existing view. B wrote its whole stale struct, FPS=true included, so the next launch lost A's
// choice. Each toggle persists only its own key.
TEST(PlaybackInfoLabel, InfoTwoR2A6EachToggleWritesOnlyItsOwnSettingsKey)
{
    const QString dir = QDir::tempPath() + QStringLiteral("/mlvapp-info2r2a6-") + QUuid::createUuid().toString(QUuid::WithoutBraces);
    ASSERT_FALSE(automation_settings::isolate(dir).isEmpty());
    // instance B loaded all six on (its in-memory copy), then A saved FPS off, then B saves Dual-ISO off
    const playback_info_label::Toggles instanceB = togglesFromMask(0x3F);
    ASSERT_TRUE(instanceB.fps);
    playback_info_label::toggleWriteToSettings(&playback_info_label::Toggles::fps, false);           // instance A
    playback_info_label::toggleWriteToSettings(&playback_info_label::Toggles::dualIso, false);       // instance B
    const playback_info_label::Toggles next = playback_info_label::togglesFromSettings();
    ASSERT_FALSE(next.fps);        // A's choice survives B's save
    ASSERT_FALSE(next.dualIso);
    ASSERT_TRUE(next.pipeline && next.scale && next.debayer && next.resolution);
    // each member maps to its own key, and a non-toggle member to none
    ASSERT_QSTR_EQ(QString::fromLatin1(playback_info_label::Settings::kKeyShowResolution()),
                   QString::fromLatin1(playback_info_label::toggleSettingsKey(&playback_info_label::Toggles::resolution)));
    ASSERT_QSTR_EQ(QString::fromLatin1(playback_info_label::Settings::kKeyShowPipeline()),
                   QString::fromLatin1(playback_info_label::toggleSettingsKey(&playback_info_label::Toggles::pipeline)));
    automation_settings::releaseForTest();
    QDir(dir).removeRecursively();
    // the wiring: the toggle handler writes its own choice, never the whole struct
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString handler = functionBody(source, QStringLiteral("void MainWindow::setPlaybackInfoToggle("));
    ASSERT_TRUE(handler.contains(QStringLiteral("playback_info_label::toggleWriteToSettings( field, on );")));
    ASSERT_FALSE(handler.contains(QStringLiteral("togglesWriteToSettings")));
    const QString policy = readRepoFile(QStringLiteral("platform/qt/PlaybackInfoLabelPolicy.h"));
    ASSERT_FALSE(policy.contains(QStringLiteral("inline void togglesWriteToSettings(")));
}

// INFO-2 r2 (adversary pass 1 A7 on b056b902, introduced, major): fullscreen GPU window, a paused idle tick, the first Play frame
// takes 900 ms, Resolution is disabled at 100 ms. The 250 ms relabel only stored the text for the next paint; the stall rule needs an
// earlier draw, so the old text stayed on screen until the first new paint. The stored text is owed a paint: INFO-2 pass 2 A1 (on
// 9496cb72) showed the first answer (a presentation-age rule) failed when a paused scrub had just presented a frame (30 ms old at
// the flush), so the debt is armed by the store itself and a frame presented SINCE the store is what clears it.
TEST(PlaybackInfoLabel, InfoTwoR2A7ADeferredPlayingRelabelIsVisibleWithinTheGatePlusATickWhenNoFrameArrives)
{
    // the policy: owed, no frame since the store, and the debt old enough (INFO-2 r4 A3: relative to the expected frame period)
    // the first frame of a Play at 24 fps (meter EMA 0): expected 42 ms, deadline max(50, 42 + 21) = 63
    ASSERT_TRUE(playback_info_label::labelPaintOwedDue(true, false, 63, 42));
    ASSERT_FALSE(playback_info_label::labelPaintOwedDue(true, false, 62, 42));   // a steady 24 fps Play's frame comes sooner
    ASSERT_TRUE(playback_info_label::labelPaintOwedDue(true, false, 900, 42));
    ASSERT_FALSE(playback_info_label::labelPaintOwedDue(true, true, 900, 42));    // a frame presented since the store carried the text
    ASSERT_FALSE(playback_info_label::labelPaintOwedDue(false, false, 900, 42));  // nothing stored
    ASSERT_TRUE(playback_info_label::labelPaintOwedDue(true, false, playback_info_label::kLabelPaintOwedMs, 0));   // no expected period known
    ASSERT_TRUE(playback_info_label::kLabelPaintOwedMs > 42);                 // one frame period at 24 fps never pays it
    // pass 2 A1's input: relabel paused at 0, a paused scrub presents at 220 (its age is 30 ms at the flush), Play at 230, flush at
    // 250 stores the text, the first Play frame is due at 1130. The debt is armed at 250 by the store, whatever the last
    // presentation's age was, so it is due by 250 + kLabelPaintOwedMs (a tick later), 800 ms before the frame.
    ASSERT_TRUE(playback_info_label::deferredRelabelDue(true, true, 250));
    const int storeAtMs = 250;
    ASSERT_TRUE(playback_info_label::labelPaintOwedDue(true, false, (storeAtMs + playback_info_label::kLabelPaintOwedMs + 8) - storeAtMs, 0));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString push = functionBody(source, QStringLiteral("void MainWindow::pushLookLabelOverlay("));
    ASSERT_TRUE(push.contains(QStringLiteral(
        "    if( repaintNow || !gpuWindow ) m_lookLabelPaintOwedClock.invalidate();\n"
        "    else\n"
        "    {\n"
        "        m_lookLabelPaintOwedClock.start();\n"
        "        m_lookLabelPaintOwedPresentedCount = m_playbackPresentedFrameCount;\n"
        "    }")));
    const QString settle = functionBody(source, QStringLiteral("void MainWindow::settleLookLabelPaintIfOwed( void )"));
    ASSERT_TRUE(settle.contains(QStringLiteral(
        "const bool framePresentedSinceStore = m_playbackPresentedFrameCount != m_lookLabelPaintOwedPresentedCount;")));
    ASSERT_TRUE(settle.contains(QStringLiteral("if( !ui->actionPlay->isChecked() || framePresentedSinceStore )")));
    ASSERT_TRUE(settle.contains(QStringLiteral(
        "if( playback_info_label::labelPaintOwedDue( owed, framePresentedSinceStore, m_lookLabelPaintOwedClock.elapsed(),\n"
        "                                                playback_info_label::labelPaintExpectedFrameMs( getPlaybackFramerate(), m_playbackFpsEmaFrameMs ) ) )")));
    ASSERT_TRUE(settle.contains(QStringLiteral("GpuDisplayWindow::setLookLabelIfActive( m_lookLabelOverlayText, true );")));
    // every tick, right after the gated flush
    const QString tick = functionBody(source, QStringLiteral("void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )"));
    const int flushAt = tick.indexOf(QStringLiteral("flushPlaybackInfoIfDue();"));
    ASSERT_TRUE(flushAt >= 0);
    ASSERT_TRUE(tick.indexOf(QStringLiteral("settleLookLabelPaintIfOwed();"), flushAt) > flushAt);
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("QElapsedTimer m_lookLabelPaintOwedClock;")));
    ASSERT_FALSE(source.contains(QStringLiteral("m_playbackPresentedClock")));   // the first answer's presentation-age clock is gone
}

// INFO-2 r4 (adversary r3 A3 on ce4a1a34): a native 10 fps clip in steady Play. The text is stored at 210 ms and the next frame is due
// at 310 ms; the fixed 50 ms debt paid an extra label paint at 264 ms on every relabel, so a steady Play under 20 fps added a paint
// of its own. The debt is relative to the expected frame period (the larger of the target period and the measured meter EMA), with
// half a period of margin, and never below kLabelPaintOwedMs.
TEST(PlaybackInfoLabel, InfoTwoR4A3TheLabelPaintDebtIsRelativeToTheExpectedFramePeriod)
{
    // R1a: the adversary's literal, stored at 210, checked at 264 (54 ms), 10 fps expected period 100 ms: no paint owed yet
    ASSERT_FALSE(playback_info_label::labelPaintOwedDue(true, false, 264 - 210, 100));
    // R1b: at 10 fps a real stall still pays, at 100 + 50 = 150 ms
    ASSERT_FALSE(playback_info_label::labelPaintOwedDue(true, false, 149, 100));
    ASSERT_TRUE(playback_info_label::labelPaintOwedDue(true, false, 150, 100));
    // R1c: the expected period is the larger of the target period and the measured EMA
    ASSERT_EQ(100, playback_info_label::labelPaintExpectedFrameMs(24.0, 100.0));   // a 24 fps target that plays at 10 fps
    ASSERT_EQ(42, playback_info_label::labelPaintExpectedFrameMs(24.0, 0.0));
    ASSERT_EQ(100, playback_info_label::labelPaintExpectedFrameMs(10.0, 0.0));
    ASSERT_EQ(0, playback_info_label::labelPaintExpectedFrameMs(0.0, 0.0));
}

// INFO-2 r2 (adversary pass 2 A2 on 9496cb72, introduced, major): 24 fps, start on paused frame 0, Cut Out 3, Loop off. Frames 1 and
// 2 hand off at 8 and 117 ms; the frame-2 request consumes the first stamp, then the next advance stops Play. The Play toggle set the
// meter's seen count to the presentation count at the STOP as well as at the start, dropping the final 109 ms interval. It does so
// at the start only.
TEST(PlaybackInfoLabel, InfoTwoR2A2TheStopOfAPlayKeepsThePresentationsTheMeterHasNotSteppedOn)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString toggled = functionBody(source, QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"));
    ASSERT_FALSE(toggled.isEmpty());
    ASSERT_TRUE(toggled.contains(QStringLiteral(
        "if( checked ) m_playbackFpsMeterSeenPresentedCount = m_playbackPresentedFrameCount;")));
    ASSERT_FALSE(toggled.contains(QStringLiteral("\n    m_playbackFpsMeterSeenPresentedCount = m_playbackPresentedFrameCount;")));
    // the advisory: the span helper's rounding addition does not overflow int
    ASSERT_EQ(1073741824, playback_fps_meter::presentedIntervalMs(2147483647, 2));
}

// INFO-2 r2 (adversary pass 1 A8 on b056b902, introduced, major): a 60 fps clip conformed to 24, stalled at "--/60 fps (60->24)";
// 30 is selected with no conform override and no further presentation. The status meter refreshed but the corner stayed at 60->24
// because the relabel key never saw the conform rate. The rate is in the key and the conform funnel asks for the relabel, whatever
// the measured fps does.
TEST(PlaybackInfoLabel, InfoTwoR2A8TheCornerRelabelsOnAConformRateChangeIndependentlyOfMeasuredFps)
{
    playback_info_label::Facts f = ownerFacts();
    f.clipFps = 60.0;
    f.playing = true;
    f.achievedFps = 0.0;   // stalled: "--"
    f.playbackRate = 24.0;
    ASSERT_QSTR_EQ(QStringLiteral("--/60 fps (60->24)"), playback_info_label::fpsField(f));
    f.playbackRate = 30.0;
    ASSERT_QSTR_EQ(QStringLiteral("--/60 fps (60->30)"), playback_info_label::fpsField(f));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString key = functionBody(source, QStringLiteral("void MainWindow::refreshPlaybackInfoOnInputChange("));
    ASSERT_TRUE(key.contains(QStringLiteral("inputs.playbackRate = m_pMlvObject ? getPlaybackFramerate() : 0.0;")));
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("&& playbackRate == o.playbackRate")));
    const QString apply = functionBody(source, QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"));
    const int statusAt = apply.indexOf(QStringLiteral("refreshPlaybackFpsStatus();"));
    ASSERT_TRUE(statusAt >= 0);
    const int relabelAt = apply.indexOf(QStringLiteral("refreshPlaybackInfoOnInputChange( infoStatus, m_playbackInfoInputs.scopeCaveat );"), statusAt);
    ASSERT_TRUE(relabelAt > statusAt);
    // nothing in the relabel reads a measured rate (the key is the conform rate, not the achieved fps)
    ASSERT_FALSE(key.contains(QStringLiteral("m_playbackInfoFpsShown")));
}
