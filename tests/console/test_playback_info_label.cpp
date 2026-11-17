// LOOK-LABEL-PLAYBACK-INFO-1: the playback-info lines under the fullscreen look label. Owner, 2026-11-06 (PR 459):
// "We need to show other useful info as well. CPU vs GPU. Scale (Full, Half, Quarter)." Owner, 2026-11-16: "show other
// useful info such as fps, resolution, etc". compose() is pure (PlaybackInfoLabelPolicy.h); MainWindow, the surfaces and
// the receipt are not linked into console_tests, so their wiring is pinned as source text.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackInfoLabelPolicy.h"
#include "../../platform/qt/PlaybackScaleLabel.h"

#include <QChar>
#include <QFile>
#include <QString>
#include <QTextStream>

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
    ASSERT_TRUE(facts.contains(QStringLiteral("m_pChosenDebayer->text()")));
    ASSERT_FALSE(facts.contains(QStringLiteral("playbackDebayerLabel(")));
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
    // Written at the gate (beside m_pFpsStatus) and at the reset, nowhere else.
    ASSERT_EQ(2, static_cast<int>(source.count(QStringLiteral("m_playbackInfoFpsShown = "))));
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
    ASSERT_TRUE(key.contains(QStringLiteral("m_lastPresentedPlaybackScaleFactorActive")));
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
    ASSERT_TRUE(facts.contains(QStringLiteral(
        "facts.dualIsoOn = llrpGetFixRawMode( m_pMlvObject ) != 0 && llrpGetDualIsoMode( m_pMlvObject ) != 0;")));
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
