// VENUE-LEG-FULLSCREEN-TOGGLE-DURING-PLAY-1: pins the MLVAPP_GUI_SMOKE_MID_PLAY_WINDOW_TOGGLES grammar
// (SmokeWindowToggleSchedule.h) and, by source scan (MainWindow.cpp needs a full GUI build, so it is read as text,
// mirroring test_resize_playback_policy.cpp), that the smoke hook is wired default-off, windowed-only, and never
// starts or stops Play.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/SmokeWindowToggleSchedule.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QTextStream>

#include <string>

namespace sched = smoke_window_toggle_schedule;

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    if (path.isEmpty()) return QString();
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) return QString();
    QTextStream stream(&file);
    return stream.readAll();
}

void expectRejected(const char * text, const char * reason)
{
    const sched::Schedule s = sched::parse(text);
    ASSERT_FALSE(s.ok());
    ASSERT_TRUE(s.entries.empty());
    ASSERT_EQ(std::string(reason), s.reason);
}

// A MainWindow function body by brace matching, // comments removed (empty when the signature is absent).
QString mainWindowFunction(const QString & signature)
{
    QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    source.remove(QRegularExpression(QStringLiteral("//[^\\n]*")));
    const int at = source.indexOf(signature);
    if (at < 0) return QString();
    const int open = source.indexOf(QLatin1Char('{'), at);
    if (open < 0) return QString();
    int depth = 0;
    for (int i = open; i < source.size(); ++i)
    {
        if (source.at(i) == QLatin1Char('{')) ++depth;
        else if (source.at(i) == QLatin1Char('}') && --depth == 0) return source.mid(at, i + 1 - at);
    }
    return QString();
}

} // namespace

TEST(SmokeWindowToggleSchedule, TheCardScheduleParsesInOrder)
{
    const sched::Schedule s = sched::parse("fs_on@4000,fs_off@8000,restore@12000,maximize@16000");
    ASSERT_TRUE(s.ok());
    ASSERT_EQ(4, static_cast<int>(s.entries.size()));
    ASSERT_EQ(std::string("fs_on"), std::string(sched::kindName(s.entries[0].kind)));
    ASSERT_EQ(4000, s.entries[0].atMs);
    ASSERT_EQ(std::string("fs_off"), std::string(sched::kindName(s.entries[1].kind)));
    ASSERT_EQ(std::string("restore"), std::string(sched::kindName(s.entries[2].kind)));
    ASSERT_EQ(std::string("maximize"), std::string(sched::kindName(s.entries[3].kind)));
    ASSERT_EQ(16000, s.entries[3].atMs);
}

TEST(SmokeWindowToggleSchedule, ASingleEntryAndZeroMsAreValid)
{
    const sched::Schedule s = sched::parse("maximize@0");
    ASSERT_TRUE(s.ok());
    ASSERT_EQ(1, static_cast<int>(s.entries.size()));
    ASSERT_EQ(0, s.entries[0].atMs);
}

TEST(SmokeWindowToggleSchedule, TheMinimumGapIsAccepted)
{
    ASSERT_TRUE(sched::parse("fs_on@1000,fs_off@3500").ok());
    ASSERT_EQ(2000, sched::kSettleMs);
    ASSERT_EQ(2500, sched::kMinGapMs);
}

TEST(SmokeWindowToggleSchedule, AnyMalformedEntryRejectsTheWholeSchedule)
{
    expectRejected("", "empty_schedule");
    expectRejected("fs_on@4000,", "empty_entry");
    expectRejected(",fs_on@4000", "empty_entry");
    expectRejected("fs_on@4000,,fs_off@8000", "empty_entry");
    expectRejected("fs_on4000", "missing_at_sign");
    expectRejected("fullscreen@4000", "unknown_kind");
    expectRejected("FS_ON@4000", "unknown_kind");
    expectRejected("@4000", "unknown_kind");
    expectRejected("fs_on@", "bad_ms");
    expectRejected("fs_on@-1", "bad_ms");
    expectRejected("fs_on@40x0", "bad_ms");
    expectRejected("fs_on@ 4000", "bad_ms");
    expectRejected("fs_on@4.5", "bad_ms");
    expectRejected("fs_on@1234567890", "bad_ms");
    // one good entry before a bad one still rejects everything
    expectRejected("fs_on@4000,bogus@8000", "unknown_kind");
}

TEST(SmokeWindowToggleSchedule, OrderAndGapAreEnforced)
{
    expectRejected("fs_on@8000,fs_off@4000", "not_ascending");
    expectRejected("fs_on@4000,fs_off@4000", "not_ascending");
    expectRejected("fs_on@4000,fs_off@6499", "gap_too_small");
    expectRejected("fs_on@4000,fs_off@6000", "gap_too_small");
}

TEST(SmokeWindowToggleScheduleWiring, TheHookIsDefaultOffWindowedOnlyAndLogsItsPhases)
{
    const QString body = mainWindowFunction(QStringLiteral("int MainWindow::runGuiPlaybackSmoke("));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_EQ(1, static_cast<int>(body.count(QStringLiteral("MLVAPP_GUI_SMOKE_MID_PLAY_WINDOW_TOGGLES"))));
    ASSERT_EQ(1, static_cast<int>(body.count(QStringLiteral("smoke_window_toggle_schedule::parse("))));
    ASSERT_TRUE(body.contains(QStringLiteral("reason=not_windowed")));
    ASSERT_TRUE(body.contains(QStringLiteral("reason=invalid_schedule")));
    ASSERT_TRUE(body.contains(QStringLiteral("gui_smoke.window_toggle phase=begin")));
    ASSERT_TRUE(body.contains(QStringLiteral("gui_smoke.window_toggle phase=end")));
    ASSERT_TRUE(body.contains(QStringLiteral("gui_smoke.window_toggles requested=")));
    // the user's own path for the fullscreen kinds, then restore / maximize
    ASSERT_TRUE(body.contains(QStringLiteral("ui->actionFullscreen->trigger()")));
    ASSERT_TRUE(body.contains(QStringLiteral("showMaximized()")));
    ASSERT_TRUE(body.contains(QStringLiteral("showNormal()")));
}

TEST(SmokeWindowToggleScheduleWiring, TheToggleBlockNeverStartsOrStopsPlayAndNeverWaits)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int begin = source.indexOf(QStringLiteral("VENUE-LEG-FULLSCREEN-TOGGLE-DURING-PLAY-1 STEP BEGIN"));
    const int end = source.indexOf(QStringLiteral("VENUE-LEG-FULLSCREEN-TOGGLE-DURING-PLAY-1 STEP END"), begin);
    ASSERT_TRUE(begin >= 0);
    ASSERT_TRUE(end > begin);
    QString block = source.mid(begin, end - begin);
    block.remove(QRegularExpression(QStringLiteral("//[^\\n]*")));
    ASSERT_FALSE(block.contains(QStringLiteral("actionPlay")));
    ASSERT_FALSE(block.contains(QStringLiteral("programmaticPlay")));
    ASSERT_FALSE(block.contains(QStringLiteral("programmaticStop")));
    ASSERT_FALSE(block.contains(QStringLiteral("setPlaying")));
    // a non-blocking state machine: no nested wait inside the block
    ASSERT_FALSE(block.contains(QStringLiteral("waitGuiThreadForSmokeEvents")));
    ASSERT_FALSE(block.contains(QStringLiteral("msleep")));
    ASSERT_FALSE(block.contains(QStringLiteral("QEventLoop")));
    ASSERT_FALSE(block.contains(QStringLiteral("processEvents")));
    // at most one phase per loop turn (adversary A6 on ead7bbdb): the begin gates (previous not ended, not yet due), the settled end and the
    // not-yet-settled wait each leave the turn with a break, so an overdue next toggle begins on the next turn
    ASSERT_TRUE(block.count(QStringLiteral("break;")) >= 4);
}
