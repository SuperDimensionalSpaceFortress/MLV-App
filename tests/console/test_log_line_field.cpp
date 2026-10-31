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
    const QString viewport = readRepoText(QStringLiteral("platform/qt/GpuDisplayViewport.cpp"));
    ASSERT_FALSE(viewport.contains(QStringLiteral("qgetenv(\"MLVAPP_VIEWPORT_AB_SWAP\").constData()")));
    ASSERT_FALSE(viewport.contains(QStringLiteral("qgetenv(\"MLVAPP_VIEWPORT_AB_FLUSH\").constData()")));
}
