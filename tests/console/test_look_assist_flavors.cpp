// LOOK-ASSIST-FLAVORS-1: the Classic | Cinematic Look Assist flavors.
//
// Pinned here: (1) Classic is master's preset on a 5808-line grid (the golden hash was dumped from an UNCHANGED
// master tree, fork/master b5751928, with the same grid header) and the default parameter is Classic; (2) Cinematic
// changes ONLY the six documented sliders, by exactly the one table, deterministically, and never the white
// balance deltas; (3) the selector's layers and its unknown-value rule; (4) the wiring: every preset call in the two
// consumers passes the flavor, the scene verdict stays flavor-blind, the GUI selector and the environment both
// reach the analysis, the receipt element is written only for a non-Classic flavor and is read back.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "look_assist_flavor_grid.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../src/batch/LookAssistAnalysis.h"
#include "../../src/batch/ReceiptLoader.h"

#include <QByteArray>
#include <QCryptographicHash>
#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QTemporaryDir>
#include <QTextStream>

using namespace lookassist;

namespace
{

// sha256 of look_assist_flavor_grid::dump() against fork/master b5751928's presetForLookAssistScene.
const char kMasterGridSha256[] = "a6d5eacf05a96040cdd2e9d461cf4d6680f205129c96fc2b735d57f3bde1e76f";
const int kMasterGridLines = 5808;

QString readSource( const QString &relativePath )
{
    const QString path = repo_file_path( relativePath );
    QFile file( path );
    if( path.isEmpty() || !file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return QString();
    QTextStream stream( &file );
    return stream.readAll();
}

int clampInt( int lo, int v, int hi ) { return v < lo ? lo : ( v > hi ? hi : v ); }

QString sha256Hex( const QByteArray &bytes )
{
    return QString::fromLatin1( QCryptographicHash::hash( bytes, QCryptographicHash::Sha256 ).toHex() );
}

LookAssistPreset classicDefault( LookAssistScene scene, const LookAssistStats &s,
                                 const LookAssistStats *c, const LookAssistStats *d )
{
    return presetForLookAssistScene( scene, s, c, d );
}

LookAssistPreset classicExplicit( LookAssistScene scene, const LookAssistStats &s,
                                  const LookAssistStats *c, const LookAssistStats *d )
{
    return presetForLookAssistScene( scene, s, c, d, LookAssistFlavor::Classic );
}

LookAssistPreset cinematic( LookAssistScene scene, const LookAssistStats &s,
                            const LookAssistStats *c, const LookAssistStats *d )
{
    return presetForLookAssistScene( scene, s, c, d, LookAssistFlavor::Cinematic );
}

// Restores MLVAPP_LOOK_ASSIST_FLAVOR on scope exit.
class FlavorEnvGuard
{
public:
    FlavorEnvGuard() : m_wasSet( qEnvironmentVariableIsSet( "MLVAPP_LOOK_ASSIST_FLAVOR" ) ),
                       m_value( qgetenv( "MLVAPP_LOOK_ASSIST_FLAVOR" ) ) {}
    ~FlavorEnvGuard()
    {
        if( m_wasSet ) qputenv( "MLVAPP_LOOK_ASSIST_FLAVOR", m_value );
        else qunsetenv( "MLVAPP_LOOK_ASSIST_FLAVOR" );
    }
private:
    bool m_wasSet;
    QByteArray m_value;
};

// The count of presetForLookAssistScene( calls in `source` whose statement (up to the first ';') lacks `needle`.
int presetCallsWithout( const QString &source, const QString &needle )
{
    int missing = 0;
    int from = 0;
    const QString call = QStringLiteral("presetForLookAssistScene(");
    for( ;; )
    {
        const int at = source.indexOf( call, from );
        if( at < 0 ) break;
        from = at + call.size();
        const int end = source.indexOf( QLatin1Char(';'), at );
        if( !source.mid( at, end - at ).contains( needle ) ) ++missing;
    }
    return missing;
}

} // namespace

TEST(LookAssistFlavors, ClassicIsMastersPresetOnTheWholeGrid)
{
    const QByteArray byDefault = look_assist_flavor_grid::dump( classicDefault );
    const QByteArray byExplicit = look_assist_flavor_grid::dump( classicExplicit );
    ASSERT_EQ( kMasterGridLines, static_cast<int>( byDefault.count( '\n' ) ) );
    ASSERT_TRUE( sha256Hex( byDefault ) == QLatin1String( kMasterGridSha256 ) );
    ASSERT_TRUE( byDefault == byExplicit );
}

TEST(LookAssistFlavors, CinematicTableIsPinned)
{
    struct Row { LookAssistScene scene; int exposure, contrast, pivot, shadows, highlights, vibrance; };
    const Row rows[] = {
        { LookAssistScene::Night,             0, 20, -3, -10, -10, 4 },
        { LookAssistScene::ArtificialLights,  0, 32, -5, -14, -12, 5 },
        { LookAssistScene::Shade,             0, 40, -5, -20, -15, 6 },
        { LookAssistScene::BrightSun,         0, 30, -5, -12, -10, 5 },
    };
    for( const Row &r : rows )
    {
        const LookAssistFlavorDeltas d = lookAssistCinematicDeltasForScene( r.scene );
        ASSERT_EQ( r.exposure, d.exposure );
        ASSERT_EQ( r.contrast, d.contrast );
        ASSERT_EQ( r.pivot, d.pivot );
        ASSERT_EQ( r.shadows, d.shadows );
        ASSERT_EQ( r.highlights, d.highlights );
        ASSERT_EQ( r.vibrance, d.vibrance );
    }
}

TEST(LookAssistFlavors, CinematicChangesOnlyTheDocumentedSlidersByTheTable)
{
    const std::vector<LookAssistStats> grid = look_assist_flavor_grid::statsGrid();
    LookAssistStats display;
    display.median = 50.0;
    display.p99 = 120.0;
    int moved = 0;
    for( const LookAssistStats &s : grid )
        for( int sceneIndex = 0; sceneIndex < 4; ++sceneIndex )
            for( int useColor = 0; useColor < 2; ++useColor )
                for( int useDisplay = 0; useDisplay < 2; ++useDisplay )
                {
                    const LookAssistScene scene = static_cast<LookAssistScene>( sceneIndex );
                    const LookAssistStats *color = useColor ? &s : nullptr;
                    const LookAssistStats *disp = useDisplay ? &display : nullptr;
                    const LookAssistPreset classic = classicDefault( scene, s, color, disp );
                    const LookAssistPreset cine = cinematic( scene, s, color, disp );
                    const LookAssistFlavorDeltas d = lookAssistCinematicDeltasForScene( scene );

                    // White balance is Look Assist's decision, identical in both flavors.
                    ASSERT_EQ( classic.temperatureDelta, cine.temperatureDelta );
                    ASSERT_EQ( classic.tintDelta, cine.tintDelta );

                    // The six sliders move by exactly the table, then the documented bounds.
                    int expectedExposure = qBound( -180, classic.exposure + d.exposure, 380 );
                    if( scene == LookAssistScene::BrightSun ) expectedExposure = qMin( expectedExposure, 0 );
                    if( scene == LookAssistScene::Night ) expectedExposure = qMax( expectedExposure, 0 );
                    ASSERT_EQ( expectedExposure, cine.exposure );
                    ASSERT_EQ( clampInt( -100, classic.contrast + d.contrast, 100 ), cine.contrast );
                    ASSERT_EQ( clampInt( 0, classic.pivot + d.pivot, 100 ), cine.pivot );
                    ASSERT_EQ( clampInt( -100, classic.shadows + d.shadows, 100 ), cine.shadows );
                    ASSERT_EQ( clampInt( -100, classic.highlights + d.highlights, 100 ), cine.highlights );
                    ASSERT_EQ( clampInt( -100, classic.vibrance + d.vibrance, 100 ), cine.vibrance );

                    // Deterministic.
                    const LookAssistPreset again = cinematic( scene, s, color, disp );
                    ASSERT_TRUE( again.exposure == cine.exposure && again.contrast == cine.contrast
                              && again.pivot == cine.pivot && again.shadows == cine.shadows
                              && again.highlights == cine.highlights && again.vibrance == cine.vibrance );
                    if( cine.contrast != classic.contrast || cine.highlights != classic.highlights ) ++moved;
                }
    // It is a different grade, not a no-op.
    ASSERT_TRUE( moved > 0 );
}

TEST(LookAssistFlavors, CinematicKeepsTheSceneLimitsOfTheSceneItGrades)
{
    // Night is a rescue lift (never below 0); BrightSun never lifts (never above 0); no flavor leaves the slider range.
    const std::vector<LookAssistStats> grid = look_assist_flavor_grid::statsGrid();
    for( const LookAssistStats &s : grid )
    {
        ASSERT_TRUE( cinematic( LookAssistScene::Night, s, nullptr, nullptr ).exposure >= 0 );
        ASSERT_TRUE( cinematic( LookAssistScene::BrightSun, s, nullptr, nullptr ).exposure <= 0 );
        for( int sceneIndex = 0; sceneIndex < 4; ++sceneIndex )
        {
            const LookAssistPreset p = cinematic( static_cast<LookAssistScene>( sceneIndex ), s, nullptr, nullptr );
            ASSERT_TRUE( p.exposure >= -180 && p.exposure <= 380 );
            ASSERT_TRUE( p.contrast >= -100 && p.contrast <= 100 );
            ASSERT_TRUE( p.pivot >= 0 && p.pivot <= 100 );
            ASSERT_TRUE( p.shadows >= -100 && p.shadows <= 100 );
            ASSERT_TRUE( p.highlights >= -100 && p.highlights <= 100 );
            ASSERT_TRUE( p.vibrance >= -100 && p.vibrance <= 100 );
        }
    }
}

TEST(LookAssistFlavors, TheDocumentedTableIsTheCodeTable)
{
    // docs/look-assist-flavors.md carries the table between two markers; every row must equal the code's row.
    const QString doc = readSource( QStringLiteral("docs/look-assist-flavors.md") );
    const int begin = doc.indexOf( QStringLiteral("<!-- cinematic-table:begin -->") );
    const int end = doc.indexOf( QStringLiteral("<!-- cinematic-table:end -->") );
    ASSERT_TRUE( begin >= 0 && end > begin );
    const QString block = doc.mid( begin, end - begin );
    const struct { const char *name; LookAssistScene scene; } scenes[] = {
        { "Night", LookAssistScene::Night }, { "ArtificialLights", LookAssistScene::ArtificialLights },
        { "Shade", LookAssistScene::Shade }, { "BrightSun", LookAssistScene::BrightSun } };
    int rows = 0;
    for( const auto &s : scenes )
    {
        const QRegularExpression row( QStringLiteral(
            "^\\|\\s*%1\\s*\\|\\s*([+-]?\\d+)\\s*\\|\\s*([+-]?\\d+)\\s*\\|\\s*([+-]?\\d+)\\s*\\|\\s*([+-]?\\d+)\\s*\\|\\s*([+-]?\\d+)\\s*\\|\\s*([+-]?\\d+)\\s*\\|\\s*$" )
            .arg( QLatin1String( s.name ) ), QRegularExpression::MultilineOption );
        const QRegularExpressionMatch m = row.match( block );
        ASSERT_TRUE( m.hasMatch() );
        const LookAssistFlavorDeltas d = lookAssistCinematicDeltasForScene( s.scene );
        ASSERT_EQ( d.exposure, m.captured( 1 ).toInt() );
        ASSERT_EQ( d.contrast, m.captured( 2 ).toInt() );
        ASSERT_EQ( d.pivot, m.captured( 3 ).toInt() );
        ASSERT_EQ( d.shadows, m.captured( 4 ).toInt() );
        ASSERT_EQ( d.highlights, m.captured( 5 ).toInt() );
        ASSERT_EQ( d.vibrance, m.captured( 6 ).toInt() );
        ++rows;
    }
    ASSERT_EQ( 4, rows );
}

TEST(LookAssistFlavors, FlavorNamesAreTheOneSpelling)
{
    ASSERT_TRUE( lookAssistFlavorName( LookAssistFlavor::Classic ) == QLatin1String( "classic" ) );
    ASSERT_TRUE( lookAssistFlavorName( LookAssistFlavor::Cinematic ) == QLatin1String( "cinematic" ) );
}

TEST(LookAssistFlavors, SelectorLayersAndTheUnknownValueRule)
{
    // Nothing set: Classic from the default.
    LookAssistFlavorSelection s = lookAssistSelectFlavor( QString(), QString(), QString() );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Classic );
    ASSERT_TRUE( s.source == QLatin1String( "default" ) );
    ASSERT_FALSE( s.unknownValue );

    // Each layer alone.
    s = lookAssistSelectFlavor( QStringLiteral("cinematic"), QString(), QString() );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Cinematic && s.source == QLatin1String( "env" ) );
    s = lookAssistSelectFlavor( QString(), QStringLiteral("cinematic"), QString() );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Cinematic && s.source == QLatin1String( "receipt" ) );
    s = lookAssistSelectFlavor( QString(), QString(), QStringLiteral("cinematic") );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Cinematic && s.source == QLatin1String( "app" ) );

    // Priority: environment over receipt over app, in both directions.
    s = lookAssistSelectFlavor( QStringLiteral("classic"), QStringLiteral("cinematic"), QStringLiteral("cinematic") );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Classic && s.source == QLatin1String( "env" ) );
    s = lookAssistSelectFlavor( QStringLiteral("cinematic"), QStringLiteral("classic"), QStringLiteral("classic") );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Cinematic && s.source == QLatin1String( "env" ) );
    s = lookAssistSelectFlavor( QString(), QStringLiteral("classic"), QStringLiteral("cinematic") );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Classic && s.source == QLatin1String( "receipt" ) );

    // Case and whitespace do not matter; an empty (or blank) layer is skipped.
    s = lookAssistSelectFlavor( QStringLiteral("  CINEMATIC "), QString(), QString() );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Cinematic );
    s = lookAssistSelectFlavor( QStringLiteral("   "), QStringLiteral("Cinematic"), QString() );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Cinematic && s.source == QLatin1String( "receipt" ) );

    // Unknown: Classic, flagged, the offending value reported, and NEVER a fall through to a lower layer.
    s = lookAssistSelectFlavor( QStringLiteral("bogus"), QStringLiteral("cinematic"), QStringLiteral("cinematic") );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Classic );
    ASSERT_TRUE( s.unknownValue );
    ASSERT_TRUE( s.rejectedValue == QLatin1String( "bogus" ) );
    ASSERT_TRUE( s.source == QLatin1String( "env" ) );
    s = lookAssistSelectFlavor( QString(), QStringLiteral("cinematik"), QStringLiteral("cinematic") );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Classic && s.unknownValue );
    ASSERT_TRUE( s.source == QLatin1String( "receipt" ) );
}

TEST(LookAssistFlavors, TheEnvironmentVariableIsReadByName)
{
    FlavorEnvGuard guard;
    qunsetenv( "MLVAPP_LOOK_ASSIST_FLAVOR" );
    ASSERT_TRUE( lookAssistFlavorEnvironmentValue().isEmpty() );
    qputenv( "MLVAPP_LOOK_ASSIST_FLAVOR", "cinematic" );
    ASSERT_TRUE( lookAssistFlavorEnvironmentValue() == QLatin1String( "cinematic" ) );
    const LookAssistFlavorSelection s =
        lookAssistSelectFlavor( lookAssistFlavorEnvironmentValue(), QString(), QStringLiteral("classic") );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Cinematic && s.source == QLatin1String( "env" ) );
    qputenv( "MLVAPP_LOOK_ASSIST_FLAVOR", "nonsense" );
    const LookAssistFlavorSelection bad =
        lookAssistSelectFlavor( lookAssistFlavorEnvironmentValue(), QString(), QStringLiteral("cinematic") );
    ASSERT_TRUE( bad.flavor == LookAssistFlavor::Classic && bad.unknownValue );
}

TEST(LookAssistFlavors, ReceiptElementIsReadAndAbsentMeansNotRecorded)
{
    QTemporaryDir tempDir;
    ASSERT_TRUE( tempDir.isValid() );
    for( int withElement = 0; withElement < 2; ++withElement )
    {
        const QString path = tempDir.filePath( QStringLiteral("flavor%1.marxml").arg( withElement ) );
        QFile file( path );
        ASSERT_TRUE( file.open( QIODevice::WriteOnly | QIODevice::Text ) );
        QTextStream out( &file );
        out << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n";
        out << "<receipt version=\"4\" mlvapp=\"test\">\n";
        out << "  <exposure>34</exposure>\n";
        out << "  <lookAssistEnabled>1</lookAssistEnabled>\n";
        if( withElement ) out << "  <lookAssistFlavor>cinematic</lookAssistFlavor>\n";
        out << "  <lookAssistBaselineValid>0</lookAssistBaselineValid>\n";
        out << "</receipt>\n";
        file.close();

        ReceiptSettings receipt;
        QString error;
        ASSERT_TRUE( ReceiptLoader::loadFromFile( path, &receipt, &error ) );
        ASSERT_TRUE( receipt.lookAssistEnabled() );
        ASSERT_EQ( 34, receipt.exposure() );
        ASSERT_TRUE( receipt.lookAssistFlavor() == ( withElement ? QStringLiteral("cinematic") : QString() ) );
        // The receipt's value is a layer of the selector, below the environment.
        const LookAssistFlavorSelection s = lookAssistSelectFlavor( QString(), receipt.lookAssistFlavor(), QString() );
        ASSERT_TRUE( s.flavor == ( withElement ? LookAssistFlavor::Cinematic : LookAssistFlavor::Classic ) );
    }
    ReceiptSettings fresh;
    ASSERT_TRUE( fresh.lookAssistFlavor().isEmpty() );
}

TEST(LookAssistFlavors, EveryPresetCallInBothConsumersPassesTheFlavor)
{
    const QString applier = readSource( QStringLiteral("src/batch/ReceiptApplier.cpp") );
    const QString window = readSource( QStringLiteral("platform/qt/MainWindow.cpp") );
    ASSERT_FALSE( applier.isEmpty() );
    ASSERT_FALSE( window.isEmpty() );
    ASSERT_EQ( 0, presetCallsWithout( applier, QStringLiteral("flavor") ) );
    ASSERT_EQ( 0, presetCallsWithout( window, QStringLiteral("flavor") ) );
    ASSERT_TRUE( applier.count( QStringLiteral("presetForLookAssistScene(") ) >= 2 );
    ASSERT_TRUE( window.count( QStringLiteral("presetForLookAssistScene(") ) >= 3 );

    // The scene verdict is flavor-blind: the shared classifier's own hypothesis preset stays Classic.
    const QString analysis = readSource( QStringLiteral("src/batch/LookAssistAnalysis.cpp") );
    ASSERT_TRUE( analysis.contains( QStringLiteral(
        "presetForLookAssistScene( classifyLookAssistScene( hypothesis ), hypothesis ).exposure" ) ) );
    // resolveLookAssistScene takes no flavor at all.
    const QString header = readSource( QStringLiteral("src/batch/LookAssistAnalysis.h") );
    const int resolveAt = header.indexOf( QStringLiteral("LookAssistScene resolveLookAssistScene(") );
    ASSERT_TRUE( resolveAt >= 0 );
    ASSERT_FALSE( header.mid( resolveAt, header.indexOf( QLatin1Char(';'), resolveAt ) - resolveAt )
                      .contains( QStringLiteral("Flavor") ) );
}

TEST(LookAssistFlavors, HeadlessApplierReadsTheEnvironmentOverTheReceiptAndReportsTheFlavor)
{
    const QString applier = readSource( QStringLiteral("src/batch/ReceiptApplier.cpp") );
    ASSERT_TRUE( applier.contains( QStringLiteral(
        "lookAssistSelectFlavor(\n        lookAssistFlavorEnvironmentValue(), receipt->lookAssistFlavor(), QString() )" ) )
        || applier.contains( QStringLiteral(
        "lookAssistSelectFlavor(\r\n        lookAssistFlavorEnvironmentValue(), receipt->lookAssistFlavor(), QString() )" ) ) );
    // Appended, never inserted: the existing fields keep their order.
    ASSERT_TRUE( applier.contains( QStringLiteral("initialPatchFinalChroma=%38 %39 flavor=%40") ) );
    ASSERT_TRUE( applier.contains( QStringLiteral(
        ".arg( lookAssistDecisionLogFields( stats, decisionTrace ) )\n        .arg( lookAssistFlavorName( flavor ) ) );") ) );
    ASSERT_TRUE( applier.contains( QStringLiteral("receipt->setLookAssistFlavor( lookAssistFlavorName( flavor ) )") ) );
    ASSERT_TRUE( applier.contains( QStringLiteral("unknown flavor '%1' from %2; using classic") ) );
}

TEST(LookAssistFlavors, GuiSelectorAndEnvironmentBothReachTheAnalysis)
{
    const QString window = readSource( QStringLiteral("platform/qt/MainWindow.cpp") );
    const QString ui = readSource( QStringLiteral("platform/qt/MainWindow.ui") );
    ASSERT_TRUE( ui.contains( QStringLiteral("name=\"comboBoxLookAssistFlavor\"") ) );

    // The one resolver: environment first, the combo as the app setting.
    const int resolverAt = window.indexOf( QStringLiteral("LookAssistFlavor MainWindow::currentLookAssistFlavor()") );
    ASSERT_TRUE( resolverAt >= 0 );
    const QString resolver = window.mid( resolverAt, 900 );
    ASSERT_TRUE( resolver.contains( QStringLiteral("lookAssistFlavorEnvironmentValue()") ) );
    ASSERT_TRUE( resolver.contains( QStringLiteral("comboBoxLookAssistFlavor->currentData().toString()") ) );

    // The analysis resolves it once, right after the (flavor-blind) scene verdict, and reports it.
    ASSERT_EQ( 1, window.count( QStringLiteral("const LookAssistFlavor flavor = currentLookAssistFlavor();") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("last_frame=%27 next_serial=%28 %29 flavor=%30") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("floor_lifted=%4 flavor=%5") ) );
    // Every reporting site feeds its placeholder (flavor AFTER the #240 decision fields): the sync result, the async dispatch and the venue telemetry.
    ASSERT_TRUE( window.contains( QStringLiteral(".arg( lookAssistDecisionLogFields( stats, decisionTrace ) )\n            .arg( lookAssistFlavorName( flavor ) ) );") ) );
    ASSERT_TRUE( window.contains( QStringLiteral(".arg( bool01( floorLiftedNightThumbnail ) )\n                .arg( lookAssistFlavorName( flavor ) ) );") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("m_lastAppliedLookAssistFlavor = lookAssistFlavorName( flavor );") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("gpu_preview_processing_reject_reason=%47 \"\n            \"look_assist_flavor=%48\"") ) );
    ASSERT_TRUE( window.contains( QStringLiteral(".arg( m_lastLookAssistDiagnosticsValid && !m_lastAppliedLookAssistFlavor.isEmpty()\n                  ? m_lastAppliedLookAssistFlavor") ) );

    // Changing the selector re-runs Look Assist the way switching it on does; it persists as an app setting
    // (default Classic) and a receipt that declares a flavor shows it.
    const int slotAt = window.indexOf( QStringLiteral("void MainWindow::on_comboBoxLookAssistFlavor_currentIndexChanged") );
    ASSERT_TRUE( slotAt >= 0 );
    ASSERT_TRUE( window.mid( slotAt, 900 ).contains( QStringLiteral(
        "    if( ui->checkBoxLookAssistEnable->isChecked() )\n        on_checkBoxLookAssistEnable_clicked( true );" ) ) );
    ASSERT_TRUE( window.contains( QStringLiteral("set.setValue( \"lookAssistFlavor\"") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("set.value( \"lookAssistFlavor\", QString( \"classic\" ) )") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("receipt->lookAssistFlavor().trimmed().toLower()") ) );
}

TEST(LookAssistFlavors, ReceiptElementIsWrittenOnlyForANonClassicFlavor)
{
    const QString window = readSource( QStringLiteral("platform/qt/MainWindow.cpp") );
    const int at = window.indexOf( QStringLiteral("writeTextElement( \"lookAssistFlavor\"") );
    ASSERT_TRUE( at >= 0 );
    const QString guard = window.mid( qMax( 0, at - 260 ), 260 );
    ASSERT_TRUE( guard.contains( QStringLiteral("!receipt->lookAssistFlavor().isEmpty()") ) );
    ASSERT_TRUE( guard.contains( QStringLiteral("receipt->lookAssistFlavor() != QLatin1String( \"classic\" )") ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("writeTextElement( \"lookAssistFlavor\"") ) );
}
