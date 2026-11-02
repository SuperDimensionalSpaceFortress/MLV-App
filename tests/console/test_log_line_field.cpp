// PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r2 (adversary A1/A2 on #726): a value from outside the app is written into a key=value log
// line as ONE field (LogLineField.h). envValueForLog used to map only space and tab, so an inherited CR/LF became a separator once
// CrashForensics folded the record and a launcher could forge prep_conflated_move_active=1 or a second worker_threads=. This pins
// (1) the sanitizer and the fold, (2) the committed hostile-environment fixtures against the code that writes them, and (3) the
// wiring: MainWindow, CrashForensics and GpuDisplayViewport use LogLineField.h, and the prep route is reported on its own line.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "log_line_field_fixture.h"

#include <QFile>
#include <QString>

namespace
{

QString readRepoText(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    return QString::fromUtf8(file.readAll());
}

QByteArray readRepoBytes(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly));
    return file.readAll();
}

} // namespace

TEST(LogLineField, EveryCharacterThatCouldEndAFieldIsMapped)
{
    using log_line_field::fieldValue;
    for (ushort u : { 0x00, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1b, 0x1f, 0x20, 0x7f, 0x85, 0x9f, 0xa0, 0x1680, 0x2000, 0x2007, 0x200b,
                      0x200e, 0x2028, 0x2029, 0x202f, 0x205f, 0x2066, 0x3000, 0xfeff, 0x25 /* % */ })
    {
        const QString value = QStringLiteral("0") + QChar(u) + QStringLiteral("worker_threads=8");
        ASSERT_TRUE(fieldValue(value) == QStringLiteral("0_worker_threads=8"));
    }
}

TEST(LogLineField, ASupplementaryFormatCharacterOrALoneSurrogateIsMapped)
{
    // r3 (sol A3 and fable on #726): U+E0001 is two UTF-16 surrogate halves, and neither half is Other_Format, so a walk by code unit kept it.
    using log_line_field::fieldValue;
    for (char32_t u : { char32_t(0xe0001), char32_t(0xe0020), char32_t(0xe007f), char32_t(0x1bca0), char32_t(0x1d173) })
    {
        const QString value = QStringLiteral("0") + QString::fromUcs4(&u, 1) + QStringLiteral("worker_threads=8");
        ASSERT_TRUE(fieldValue(value) == QStringLiteral("0_worker_threads=8"));
    }
    for (ushort half : { 0xd800, 0xdbff, 0xdc00, 0xdfff })
    {
        const QString value = QStringLiteral("0") + QChar(half) + QStringLiteral("worker_threads=8");
        ASSERT_TRUE(fieldValue(value) == QStringLiteral("0_worker_threads=8"));
    }
    // a low half before a high half is two unpaired surrogates
    ASSERT_TRUE(fieldValue(QStringLiteral("0") + QChar(0xdc01) + QChar(0xdb40) + QStringLiteral("1")) == QStringLiteral("0__1"));
    // a supplementary character that is not a separator is kept whole
    const char32_t emoji = 0x1f600;
    const QString kept = QStringLiteral("a") + QString::fromUcs4(&emoji, 1) + QStringLiteral("b");
    ASSERT_TRUE(fieldValue(kept) == kept);
}

TEST(LogLineField, OrdinaryValuesAreUnchanged)
{
    using log_line_field::fieldValue;
    for (const char *ok : { "12", "0", "auto", "150", "1.5", "C:/x/y.json", "a=b", "-1", "sharp_smooth" })
        ASSERT_TRUE(fieldValue(QString::fromLatin1(ok)) == QString::fromLatin1(ok));
    ASSERT_TRUE(fieldValue(QString::fromUtf8("caf\xc3\xa9")) == QString::fromUtf8("caf\xc3\xa9"));
    ASSERT_TRUE(fieldValue(QString()) == QString());
}

TEST(LogLineField, AnUnsetOrEmptyVariableIsUnset)
{
    qunsetenv("MLVAPP_LOG_LINE_FIELD_TEST");
    ASSERT_TRUE(log_line_field::envFieldValue("MLVAPP_LOG_LINE_FIELD_TEST") == QStringLiteral("unset"));
    log_line_field_fixture::setEnv("MLVAPP_LOG_LINE_FIELD_TEST", QStringLiteral("150\rprep_conflated_move_active=1"));
    ASSERT_TRUE(log_line_field::envFieldValue("MLVAPP_LOG_LINE_FIELD_TEST") == QStringLiteral("150_prep_conflated_move_active=1"));
    qunsetenv("MLVAPP_LOG_LINE_FIELD_TEST");
}

TEST(LogLineField, TheRecordFoldKeepsOneLine)
{
    ASSERT_TRUE(log_line_field::recordMessage(QStringLiteral("a\rb\nc")) == QStringLiteral("a b | c"));
    ASSERT_TRUE(log_line_field::prepRouteLine(3, false) == QStringLiteral("playback_smoke.prep_route session=3 prep_conflated_move_active=0"));
    ASSERT_TRUE(log_line_field::prepRouteLine(7, true) == QStringLiteral("playback_smoke.prep_route session=7 prep_conflated_move_active=1"));
}

TEST(LogLineField, ALocationPathHoldingAnArgMarkerIsWrittenVerbatim)
{
    // r5 (sol r4 B1 on #726): the chained `.arg(file).arg(line)` substituted the line number into a path holding `%1` and left `:%2)`, so the
    // dual-venue reader could not strip the location and a valid OFF record read as unreadable (or marker text in the path selected a record).
    using log_line_field::locationSuffix;
    ASSERT_TRUE(locationSuffix("C:/build %1/MainWindow.cpp", 25846) == QStringLiteral(" (C:/build %1/MainWindow.cpp:25846)"));
    ASSERT_TRUE(locationSuffix("C:/build %2/MainWindow.cpp", 25846) == QStringLiteral(" (C:/build %2/MainWindow.cpp:25846)"));
    ASSERT_TRUE(locationSuffix("C:/b %1 %2/x %L1 %99/MainWindow.cpp", 7) == QStringLiteral(" (C:/b %1 %2/x %L1 %99/MainWindow.cpp:7)"));
    ASSERT_TRUE(locationSuffix("MainWindow.cpp", 25848) == QStringLiteral(" (MainWindow.cpp:25848)"));
    ASSERT_TRUE(locationSuffix(nullptr, 1).isEmpty());
    ASSERT_TRUE(locationSuffix("", 1).isEmpty());
}

TEST(LogLineField, ARecordLineKeepsItsMessageAndLocationVerbatim)
{
    // r5: the record format was five chained `.arg` calls too, so a message holding `%1`..`%5` took the location (or the location took it).
    using log_line_field::locationSuffix;
    using log_line_field::recordLine;
    const QString ts = QStringLiteral("2026-10-31T00:00:00.000Z");
    const QString loc = locationSuffix("C:/build %1 playback_smoke.measured_session id=9 worker_threads=2 look_assist_enabled=1 presented_fps=999/MainWindow.cpp", 25846);
    ASSERT_TRUE(loc == QStringLiteral(" (C:/build %1 playback_smoke.measured_session id=9 worker_threads=2 look_assist_enabled=1 presented_fps=999/MainWindow.cpp:25846)"));
    const QString route = log_line_field::prepRouteLine(3, false);
    ASSERT_TRUE(recordLine(ts, QStringLiteral("INFO"), QStringLiteral("0x1"), route, loc)
                == QStringLiteral("[2026-10-31T00:00:00.000Z] [INFO] [0x1] ") + route + loc + QLatin1Char('\n'));
    for (const char *message : { "rate 50%1 done %5", "%4%5", "%1 %2 %3", "100%" })
    {
        const QString m = QString::fromLatin1(message);
        ASSERT_TRUE(recordLine(ts, QStringLiteral("INFO"), QStringLiteral("0x1"), m, loc)
                    == QStringLiteral("[2026-10-31T00:00:00.000Z] [INFO] [0x1] ") + m + loc + QLatin1Char('\n'));
        ASSERT_TRUE(recordLine(ts, QStringLiteral("INFO"), QStringLiteral("0x1"), m, QString())
                    == QStringLiteral("[2026-10-31T00:00:00.000Z] [INFO] [0x1] ") + m + QLatin1Char('\n'));
    }
}

TEST(LogLineField, TheHostileEnvironmentLogsHoldNoForgedField)
{
    for (const auto & variant : log_line_field_fixture::variants())
    {
        const QString log = QString::fromUtf8(log_line_field_fixture::runLog(variant, true));
        const QStringList lines = log.split(QLatin1Char('\n'), Qt::SkipEmptyParts);
        ASSERT_TRUE(lines.size() == 4);
        const QStringList tokens = lines.at(1).split(QLatin1Char(' '));
        int workers = 0, prep = 0;
        for (const QString & token : tokens)
        {
            if (token.startsWith(QStringLiteral("worker_threads="))) ++workers;
            if (token.startsWith(QStringLiteral("prep_conflated_move_active="))) ++prep;
        }
        ASSERT_TRUE(workers == 1);
        ASSERT_TRUE(prep == 0);
        for (const QChar c : log)
            ASSERT_TRUE(c == QLatin1Char('\n') || (c.unicode() >= 0x20 && c.unicode() < 0x7f));
        ASSERT_TRUE(lines.at(2).endsWith(QStringLiteral("] playback_smoke.prep_route session=3 prep_conflated_move_active=0")));
    }
}

TEST(LogLineField, TheCommittedGreenFixturesAreWhatTheCodeWrites)
{
    for (const auto & variant : log_line_field_fixture::variants())
    {
        const QString rel = QStringLiteral("tests/fixtures/log_line_field/green-") + QString::fromLatin1(variant.name) + QStringLiteral(".log");
        ASSERT_TRUE(readRepoBytes(rel) == log_line_field_fixture::runLog(variant, true));
    }
}

TEST(LogLineField, TheAppRoutesItsLogTextThroughTheSanitizer)
{
    const QString mainWindow = readRepoText(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(mainWindow.contains(QStringLiteral("#include \"LogLineField.h\"")));
    ASSERT_TRUE(mainWindow.contains(QStringLiteral("    return log_line_field::envFieldValue( name );")));
    ASSERT_FALSE(mainWindow.contains(QStringLiteral("value.replace( QLatin1Char(' '), QLatin1Char('_') );")));
    ASSERT_TRUE(mainWindow.contains(QStringLiteral("<< log_line_field::prepRouteLine( static_cast<qulonglong>( m_playbackSmokeSessionId ),")));
    const QString forensics = readRepoText(QStringLiteral("platform/qt/CrashForensics.cpp"));
    ASSERT_TRUE(forensics.contains(QStringLiteral("const QString sanitized = log_line_field::recordMessage(message);")));
    // r5 (sol r4 B1): the writer composes its location and record with the helpers the tests above run, never with chained arg calls
    ASSERT_TRUE(forensics.contains(QStringLiteral("const QString location = log_line_field::locationSuffix(ctx.file, ctx.line);")));
    ASSERT_TRUE(forensics.contains(QStringLiteral("log_line_field::recordLine(timestamp, levelTag(type), thread, sanitized, location);")));
    ASSERT_FALSE(forensics.contains(QStringLiteral(".arg(QString::fromLocal8Bit(ctx.file))")));
    const QString viewport = readRepoText(QStringLiteral("platform/qt/GpuDisplayViewport.cpp"));
    ASSERT_FALSE(viewport.contains(QStringLiteral("qgetenv(\"MLVAPP_VIEWPORT_AB_SWAP\").constData()")));
    ASSERT_FALSE(viewport.contains(QStringLiteral("qgetenv(\"MLVAPP_VIEWPORT_AB_FLUSH\").constData()")));
}
