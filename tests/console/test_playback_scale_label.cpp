// SCALE-LABEL-CLARITY-1: pins the words a person reads for a playback scale factor.
// "scale 2" / "x4" means half / quarter resolution and was read backwards, so every
// user-facing string says the resolution instead. PlaybackScaleLabel is the one place
// the factor becomes words; the .ui menu actions are static text, so a wiring check
// below keeps them equal to what the header says (MainWindow.ui is read as text,
// MainWindow.cpp is not linked into console_tests).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackScaleLabel.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <string>

// minitest streams its operands, which a QString cannot do; compare as UTF-8 std::string.
#define ASSERT_QSTR_EQ(EXPECTED, ACTUAL) \
    ASSERT_EQ(std::string((EXPECTED).toStdString()), std::string((ACTUAL).toStdString()))

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

// The <string> of the "text" property of the named <action> in MainWindow.ui.
QString actionText(const QString & ui, const QString & actionName)
{
    const int at = ui.indexOf(QStringLiteral("<action name=\"%1\">").arg(actionName));
    const int end = ui.indexOf(QStringLiteral("</action>"), at);
    if (at < 0 || end <= at) return QString();
    const QString body = ui.mid(at, end - at);
    const QString open = QStringLiteral("<property name=\"text\">");
    const int textAt = body.indexOf(open);
    if (textAt < 0) return QString();
    const int strAt = body.indexOf(QStringLiteral("<string>"), textAt);
    const int strEnd = body.indexOf(QStringLiteral("</string>"), strAt);
    if (strAt < 0 || strEnd <= strAt) return QString();
    const int from = strAt + int(QStringLiteral("<string>").size());
    return body.mid(from, strEnd - from);
}

} // namespace

TEST(PlaybackScaleLabel, ResolutionLabelNamesTheResolutionForEachFactor)
{
    ASSERT_QSTR_EQ(QStringLiteral("Full res (1x)"), PlaybackScaleLabel::resolutionLabel(1));
    ASSERT_QSTR_EQ(QStringLiteral("Half res (0.5x)"), PlaybackScaleLabel::resolutionLabel(2));
    ASSERT_QSTR_EQ(QStringLiteral("Quarter res (0.25x)"), PlaybackScaleLabel::resolutionLabel(4));
    ASSERT_QSTR_EQ(QStringLiteral("Custom res (1/8)"), PlaybackScaleLabel::resolutionLabel(8));
}

TEST(PlaybackScaleLabel, ShortLabelIsTheCompactToolbarForm)
{
    ASSERT_QSTR_EQ(QStringLiteral("Full res"), PlaybackScaleLabel::shortLabel(1));
    ASSERT_QSTR_EQ(QStringLiteral("Half res"), PlaybackScaleLabel::shortLabel(2));
    ASSERT_QSTR_EQ(QStringLiteral("Quarter res"), PlaybackScaleLabel::shortLabel(4));
    ASSERT_QSTR_EQ(QStringLiteral("1/8 res"), PlaybackScaleLabel::shortLabel(8));
}

TEST(PlaybackScaleLabel, IndicatorTextIsTheShortLabelUnlessActiveDiffersFromRequested)
{
    // SCALE-LABEL-INDICATOR-PIN-1: the toolbar indicator never falls back to the bare "x%1" factor form.
    ASSERT_QSTR_EQ(QStringLiteral("Full res"), PlaybackScaleLabel::indicatorText(1, 1));
    ASSERT_QSTR_EQ(QStringLiteral("Half res"), PlaybackScaleLabel::indicatorText(2, 2));
    ASSERT_QSTR_EQ(QStringLiteral("Quarter res"), PlaybackScaleLabel::indicatorText(4, 4));
    ASSERT_QSTR_EQ(QStringLiteral("1/8 res"), PlaybackScaleLabel::indicatorText(8, 8));
    ASSERT_QSTR_EQ(QStringLiteral("Full res->Half res"), PlaybackScaleLabel::indicatorText(1, 2));
    ASSERT_QSTR_EQ(QStringLiteral("Half res->Quarter res"), PlaybackScaleLabel::indicatorText(2, 4));
    ASSERT_QSTR_EQ(QStringLiteral("Quarter res->1/8 res"), PlaybackScaleLabel::indicatorText(4, 8));
}

TEST(PlaybackScaleLabel, MainWindowIndicatorUsesTheHelperNotAnInlineFormat)
{
    // MainWindow.cpp is not linked into console_tests, so read it as text: the call site must go through indicatorText.
    const QString src = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(src.contains(QStringLiteral("PlaybackScaleLabel::indicatorText(")));
    ASSERT_FALSE(src.contains(QStringLiteral("tr( \"x%1\" )")));
}

TEST(PlaybackScaleLabel, SessionReasonLogUsesTheCSeamsWordingNotTheUiLabel)
{
    // SCALE-LABEL-TELEMETRY-REASON-CONSISTENCY-1: MainWindow's session_reason / playback_scale_gpu_texture_route_session reason must read exactly
    // what the C seam (mlvDualIsoGpuPreviewScaleReconPlan) writes for the same condition. Neither file is linked into console_tests, so read both as text.
    const QString seam = readRepoFile(QStringLiteral("src/mlv/video_mlv.c"));
    const QString window = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(seam.contains(QStringLiteral("plan->reason = \"x8 stays on the full-resolution texture route\";")));
    ASSERT_TRUE(seam.contains(QStringLiteral("plan->reason = \"preview scale 1 (full resolution)\";")));
    ASSERT_TRUE(window.contains(QStringLiteral("QStringLiteral(\"x%1 stays on the full-resolution texture route\")")));
    ASSERT_TRUE(window.contains(QStringLiteral("QStringLiteral(\"preview scale 1 (full resolution)\")")));
    ASSERT_FALSE(window.contains(QStringLiteral("resolutionLabel( requestedScale ) );")));
    ASSERT_FALSE(window.contains(QStringLiteral(" preview\")")));
}

TEST(PlaybackScaleLabel, UnexpectedFactorsStillProduceReadableText)
{
    ASSERT_QSTR_EQ(QStringLiteral("Custom res (1/3)"), PlaybackScaleLabel::resolutionLabel(3));
    ASSERT_QSTR_EQ(QStringLiteral("1/16 res"), PlaybackScaleLabel::shortLabel(16));
}

TEST(PlaybackScaleLabel, NoLabelLeadsWithTheBareFactorPeopleReadBackwards)
{
    const int factors[] = {1, 2, 4, 8};
    for (const int factor : factors) {
        const QString full = PlaybackScaleLabel::resolutionLabel(factor);
        const QString shortForm = PlaybackScaleLabel::shortLabel(factor);
        ASSERT_FALSE(full.startsWith(QLatin1Char('x')));
        ASSERT_FALSE(shortForm.startsWith(QLatin1Char('x')));
        ASSERT_FALSE(full.contains(QStringLiteral("scale"), Qt::CaseInsensitive));
        ASSERT_FALSE(shortForm.contains(QStringLiteral("scale"), Qt::CaseInsensitive));
        ASSERT_TRUE(full.contains(QStringLiteral("res")));
    }
}

TEST(PlaybackScaleLabel, MenuActionTextsInTheUiEqualTheHeaderLabels)
{
    const QString ui = readRepoFile(QStringLiteral("platform/qt/MainWindow.ui"));
    ASSERT_QSTR_EQ(PlaybackScaleLabel::resolutionLabel(1), actionText(ui, QStringLiteral("actionPlaybackScale1")));
    ASSERT_QSTR_EQ(PlaybackScaleLabel::resolutionLabel(2), actionText(ui, QStringLiteral("actionPlaybackScale2")));
    ASSERT_QSTR_EQ(PlaybackScaleLabel::resolutionLabel(4), actionText(ui, QStringLiteral("actionPlaybackScale4")));
    ASSERT_QSTR_EQ(PlaybackScaleLabel::resolutionLabel(8), actionText(ui, QStringLiteral("actionPlaybackScale8")));
}
