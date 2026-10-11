// LOOK-ASSIST-FLAVORS-1: the Classic | Cinematic Look Assist flavors.
//
// Pinned here: (1) Classic is master's preset on a 5808-line grid (the golden hash was dumped from an UNCHANGED
// master tree, fork/master b5751928, with the same grid header) and the default parameter is Classic; (2) Cinematic
// changes ONLY the five documented sliders, by exactly the one table, deterministically, and never the white
// balance deltas; (3) the selector's layers and its unknown-value rule; (4) the wiring: every preset call in the two
// consumers passes the flavor, the scene verdict stays flavor-blind, the GUI selector and the environment both
// reach the analysis, the receipt element is written only for a non-Classic flavor and is read back; (5) the receipt
// records the flavor that was APPLIED (LOOK-ASSIST-FLAVORS-2): a safety fallback leaves no Cinematic name behind.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "look_assist_flavor_grid.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../src/batch/LookAssistAnalysis.h"
#include "../../src/batch/ReceiptLoader.h"
#include "../../src/batch/ReceiptApplier.h"

#include <cmath>

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

                    // Exposure is Classic's exactly, whatever the scene limits or the display statistics make of it: the
                    // white-balance refinement renders at the preset's exposure, so a moved exposure would move the balance.
                    ASSERT_EQ( classic.exposure, cine.exposure );
                    // The five tone sliders move by exactly the table, then the documented bounds.
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

TEST(LookAssistFlavors, CinematicNeverRestatesTheExposureAndStaysInTheSliderRange)
{
    // The scene limits (Night never below 0, BrightSun never above 0) are Classic's to apply; Cinematic must not
    // re-clamp the exposure it was handed. BrightSun with display statistics median=50 p99=120 is the case that
    // used to differ: Classic's exposure there is 114, which a re-clamp to <= 0 turned into 0.
    LookAssistStats display;
    display.median = 50.0;
    display.p99 = 120.0;
    const std::vector<LookAssistStats> grid = look_assist_flavor_grid::statsGrid();
    int liftedBrightSun = 0;
    for( const LookAssistStats &s : grid )
        for( int sceneIndex = 0; sceneIndex < 4; ++sceneIndex )
            for( int useDisplay = 0; useDisplay < 2; ++useDisplay )
            {
                const LookAssistScene scene = static_cast<LookAssistScene>( sceneIndex );
                const LookAssistStats *disp = useDisplay ? &display : nullptr;
                const LookAssistPreset classic = classicDefault( scene, s, nullptr, disp );
                const LookAssistPreset p = cinematic( scene, s, nullptr, disp );
                ASSERT_EQ( classic.exposure, p.exposure );
                if( scene == LookAssistScene::BrightSun && classic.exposure > 0 ) ++liftedBrightSun;
                ASSERT_TRUE( p.exposure >= -180 && p.exposure <= 380 );
                ASSERT_TRUE( p.contrast >= -100 && p.contrast <= 100 );
                ASSERT_TRUE( p.pivot >= 0 && p.pivot <= 100 );
                ASSERT_TRUE( p.shadows >= -100 && p.shadows <= 100 );
                ASSERT_TRUE( p.highlights >= -100 && p.highlights <= 100 );
                ASSERT_TRUE( p.vibrance >= -100 && p.vibrance <= 100 );
            }
    // The grid really reaches the state the review found (otherwise the identity above proves nothing).
    ASSERT_TRUE( liftedBrightSun > 0 );
}

TEST(LookAssistFlavors, ApplyingTheFlavorAfterTheClassicPresetIsTheCinematicPreset)
{
    // The GUI's white-balance walk renders the picture, so it runs on the CLASSIC preset and Cinematic is laid over the
    // result afterwards. That is only the same grade if the overlay equals what the preset function makes.
    LookAssistStats display;
    display.median = 50.0;
    display.p99 = 120.0;
    const std::vector<LookAssistStats> grid = look_assist_flavor_grid::statsGrid();
    for( const LookAssistStats &s : grid )
        for( int sceneIndex = 0; sceneIndex < 4; ++sceneIndex )
            for( int useDisplay = 0; useDisplay < 2; ++useDisplay )
            {
                const LookAssistScene scene = static_cast<LookAssistScene>( sceneIndex );
                const LookAssistStats *disp = useDisplay ? &display : nullptr;
                const LookAssistPreset classic = classicDefault( scene, s, nullptr, disp );
                const LookAssistPreset cine = cinematic( scene, s, nullptr, disp );

                LookAssistPreset overlaid = classic;
                lookAssistApplyFlavorDeltas( &overlaid, scene, LookAssistFlavor::Cinematic );
                ASSERT_EQ( cine.exposure, overlaid.exposure );
                ASSERT_EQ( cine.contrast, overlaid.contrast );
                ASSERT_EQ( cine.pivot, overlaid.pivot );
                ASSERT_EQ( cine.shadows, overlaid.shadows );
                ASSERT_EQ( cine.highlights, overlaid.highlights );
                ASSERT_EQ( cine.vibrance, overlaid.vibrance );
                // The white balance is never the overlay's to touch.
                ASSERT_EQ( classic.temperatureDelta, overlaid.temperatureDelta );
                ASSERT_EQ( classic.tintDelta, overlaid.tintDelta );

                // Classic is a no-op.
                LookAssistPreset untouched = classic;
                lookAssistApplyFlavorDeltas( &untouched, scene, LookAssistFlavor::Classic );
                ASSERT_TRUE( untouched.exposure == classic.exposure && untouched.contrast == classic.contrast
                          && untouched.pivot == classic.pivot && untouched.shadows == classic.shadows
                          && untouched.highlights == classic.highlights && untouched.vibrance == classic.vibrance );
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

TEST(LookAssistFlavors, ChangingTheFlavorAfterAClassicApplyReRunsTheAnalysisAndGradesCinematic)
{
    // The GUI sequence, on the marker class the window itself owns: a Classic apply lands, the user picks Cinematic,
    // and the toggle's frame-ready step asks the marker whether the clip is already applied. It must not be.
    int receiptA = 0, receiptB = 0;   // two clips: only their addresses matter
    LookAssistAppliedMarker marker;
    LookAssistStats stats;
    stats.median = 90.0; stats.p05 = 30.0; stats.p95 = 160.0; stats.p99 = 190.0; stats.dynamicRange = 125.0;

    // The apply that is on screen: Classic.
    const LookAssistFlavorSelection classicSel = lookAssistSelectFlavor( QString(), QString(), QStringLiteral("classic") );
    const LookAssistPreset applied = presetForLookAssistScene( LookAssistScene::ArtificialLights, stats, nullptr, nullptr, classicSel.flavor );
    marker.markApplied( &receiptA );
    ASSERT_TRUE( marker.isApplied( &receiptA ) );
    ASSERT_FALSE( marker.isApplied( &receiptB ) );

    // The user selects Cinematic with Look Assist on: the slot asks the marker, which forgets the clip.
    const LookAssistFlavorSelection cinematicSel = lookAssistSelectFlavor( QString(), QString(), QStringLiteral("cinematic") );
    ASSERT_TRUE( cinematicSel.flavor == LookAssistFlavor::Cinematic );
    ASSERT_TRUE( marker.flavorChanged( &receiptA, true ) );
    // The dedup at the toggle's frame-ready step therefore lets the analysis run again...
    ASSERT_FALSE( marker.isApplied( &receiptA ) );
    // ...and that second run produces the Cinematic grade under the Cinematic name, not Classic's.
    const LookAssistPreset regraded = presetForLookAssistScene( LookAssistScene::ArtificialLights, stats, nullptr, nullptr, cinematicSel.flavor );
    ASSERT_TRUE( lookAssistFlavorName( cinematicSel.flavor ) == QLatin1String( "cinematic" ) );
    ASSERT_TRUE( regraded.contrast != applied.contrast );
    ASSERT_TRUE( regraded.highlights != applied.highlights );
    marker.markApplied( &receiptA );
    ASSERT_TRUE( marker.isApplied( &receiptA ) );

    // Look Assist off: nothing to re-run.
    ASSERT_FALSE( marker.flavorChanged( &receiptA, false ) );
    // A change on a clip the marker does not hold forgets nothing it holds for another clip.
    LookAssistAppliedMarker other;
    other.markApplied( &receiptB );
    ASSERT_TRUE( other.flavorChanged( &receiptA, true ) );
    ASSERT_TRUE( other.isApplied( &receiptB ) );
}

TEST(LookAssistFlavors, AnUnknownReceiptFlavorIsClassicAndWarnsNeverCinematic)
{
    // The GUI resolves the receipt element through the shared selector before it touches the combo box.
    LookAssistFlavorSelection sel;
    QString value = lookAssistSelectorValueForReceipt( QStringLiteral("cinematik"), &sel );
    ASSERT_TRUE( value == QLatin1String( "classic" ) );
    ASSERT_TRUE( sel.unknownValue );
    ASSERT_TRUE( sel.rejectedValue == QLatin1String( "cinematik" ) );
    ASSERT_TRUE( sel.flavor == LookAssistFlavor::Classic );
    ASSERT_TRUE( sel.source == QLatin1String( "receipt" ) );

    // The two real spellings, in any case and padding.
    value = lookAssistSelectorValueForReceipt( QStringLiteral("  Cinematic "), &sel );
    ASSERT_TRUE( value == QLatin1String( "cinematic" ) );
    ASSERT_FALSE( sel.unknownValue );
    value = lookAssistSelectorValueForReceipt( QStringLiteral("CLASSIC"), &sel );
    ASSERT_TRUE( value == QLatin1String( "classic" ) );
    ASSERT_FALSE( sel.unknownValue );

    // A receipt that declares nothing leaves the selector alone: an empty answer, no warning.
    value = lookAssistSelectorValueForReceipt( QString(), &sel );
    ASSERT_TRUE( value.isEmpty() );
    ASSERT_FALSE( sel.unknownValue );
    value = lookAssistSelectorValueForReceipt( QStringLiteral("   "), nullptr );
    ASSERT_TRUE( value.isEmpty() );
}

TEST(LookAssistFlavors, TheReceiptRecordsTheFlavorThatWasAppliedNotTheMerelySelected)
{
    // The GUI sequence behind setReceipt, on the holder the window owns. Look Assist is on and the selector says
    // Cinematic; what the receipt names depends on how the analysis ended.
    int clipA = 0, clipB = 0;   // two clips: only their addresses matter
    const QString selected = QStringLiteral("cinematic");
    LookAssistFlavorOutcome outcome;

    // Nothing has landed for the clip yet (Look Assist off, or the analysis is pending): the selector is the
    // clip's setting and the receipt keeps carrying it.
    ASSERT_TRUE( outcome.receiptValue( &clipA, selected ) == QLatin1String( "cinematic" ) );

    // A normal Cinematic success: the receipt names Cinematic, and the venue telemetry reads the same holder.
    outcome.begin();
    outcome.landed( &clipA, QStringLiteral("cinematic"), false );
    ASSERT_TRUE( outcome.receiptValue( &clipA, selected ) == QLatin1String( "cinematic" ) );
    ASSERT_TRUE( outcome.appliedName() == QLatin1String( "cinematic" ) );

    // The safety fallback (the global-green-cast guard says restore): the sliders are the baseline's. The receipt
    // must not name Cinematic over them -- Classic semantics, which the writer leaves out of the file.
    outcome.begin();
    outcome.landed( &clipA, QStringLiteral("cinematic"), true );
    ASSERT_TRUE( outcome.receiptValue( &clipA, selected ) == QLatin1String( "classic" ) );
    ASSERT_TRUE( outcome.appliedName().isEmpty() );
    // The fallback belongs to the clip it happened on: another clip still carries its own selector value.
    ASSERT_TRUE( outcome.receiptValue( &clipB, selected ) == QLatin1String( "cinematic" ) );

    // A new analysis forgets the fallback; a Classic success then names Classic even if the selector (env override
    // gone, combo stale) still reads Cinematic: the receipt names what was applied.
    outcome.begin();
    ASSERT_TRUE( outcome.receiptValue( &clipA, selected ) == QLatin1String( "cinematic" ) );
    outcome.landed( &clipA, QStringLiteral("classic"), false );
    ASSERT_TRUE( outcome.receiptValue( &clipA, selected ) == QLatin1String( "classic" ) );

    // Switching Look Assist off, or the clip closing, clears it.
    outcome.clear();
    ASSERT_TRUE( outcome.appliedName().isEmpty() );
    ASSERT_TRUE( outcome.receiptValue( &clipA, selected ) == QLatin1String( "cinematic" ) );
}

TEST(LookAssistFlavors, EveryReceiptStampAndEveryAnalysisOutcomeGoesThroughTheHolder)
{
    const QString window = readSource( QStringLiteral("platform/qt/MainWindow.cpp") );

    // setReceipt stamps through the holder, never the bare selector (the mutation that restores the selector fails here).
    const int at = window.indexOf( QStringLiteral("void MainWindow::setReceipt( ReceiptSettings *receipt )") );
    ASSERT_TRUE( at >= 0 );
    const int end = window.indexOf( QStringLiteral("\n}\n"), at );
    ASSERT_TRUE( end > at );
    const QString body = window.mid( at, end - at );
    ASSERT_TRUE( body.contains( QStringLiteral(
        "receipt->setLookAssistFlavor( m_lookAssistFlavorOutcome.receiptValue(" ) ) );
    ASSERT_FALSE( body.contains( QStringLiteral("receipt->setLookAssistFlavor( lookAssistFlavorName( currentLookAssistFlavor() ) )") ) );

    // The outcome is recorded at the sync result, the sync fallback, and both async sites -- the same call the
    // venue telemetry reads -- and it starts empty each analysis.
    ASSERT_EQ( 1, window.count( QStringLiteral("m_lookAssistFlavorOutcome.begin();") ) );
    ASSERT_EQ( 5, window.count( QStringLiteral("m_lookAssistFlavorOutcome.landed(") ) );   // sync x2, async x3
    ASSERT_TRUE( window.contains( QStringLiteral("m_lookAssistFlavorOutcome.landed( receipt, lookAssistFlavorName( flavor ), true );") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("m_lookAssistFlavorOutcome.landed( receipt, lookAssistFlavorName( flavor ), false );") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("m_lookAssistFlavorOutcome.appliedName()") ) );
    ASSERT_FALSE( window.contains( QStringLiteral("m_lastAppliedLookAssistFlavor") ) );
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
    // Nothing is reported as applied until an analysis lands: cleared when one starts, named only at its success site.
    ASSERT_TRUE( window.contains( QStringLiteral("m_lookAssistFlavorOutcome.begin();") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("m_lookAssistFlavorOutcome.landed( receipt, lookAssistFlavorName( flavor ), false );") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("gpu_preview_processing_reject_reason=%47 \"\n            \"look_assist_flavor=%48\"") ) );
    // The venue report names a flavor only for a look that is on screen: not after the safety fallback restored the baseline.
    ASSERT_TRUE( window.contains( QStringLiteral(".arg( m_lastLookAssistDiagnosticsValid && !m_lastLookAssistSafetyFallback\n                  && !m_lookAssistFlavorOutcome.appliedName().isEmpty()\n                  ? m_lookAssistFlavorOutcome.appliedName()") ) );

    // Changing the selector re-runs Look Assist the way switching it on does; it persists as an app setting
    // (default Classic) and a receipt that declares a flavor shows it.
    const int slotAt = window.indexOf( QStringLiteral("void MainWindow::on_comboBoxLookAssistFlavor_currentIndexChanged") );
    ASSERT_TRUE( slotAt >= 0 );
    ASSERT_TRUE( window.mid( slotAt, 1400 ).contains( QStringLiteral(
        "        on_checkBoxLookAssistEnable_clicked( true );" ) ) );
    ASSERT_TRUE( window.contains( QStringLiteral("set.setValue( \"lookAssistFlavor\"") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("set.value( \"lookAssistFlavor\", QString( \"classic\" ) )") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("lookAssistSelectorValueForReceipt( receipt->lookAssistFlavor(), &receiptFlavor )") ) );
    ASSERT_FALSE( window.contains( QStringLiteral("findData( receipt->lookAssistFlavor()") ) );
    // The slot goes through the marker, then the same path as switching Look Assist on.
    ASSERT_TRUE( window.mid( slotAt, 1400 ).contains( QStringLiteral("m_lookAssistApplied.flavorChanged(") ) );
    ASSERT_FALSE( window.contains( QStringLiteral("m_lookAssistAppliedReceipt") ) );
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

// ---- LOOK-ASSIST-FILM-FLAVOR-1: the Film grade ----

namespace
{

LookAssistPreset film( LookAssistScene scene, const LookAssistStats &s,
                       const LookAssistStats *c, const LookAssistStats *d )
{
    return presetForLookAssistScene( scene, s, c, d, LookAssistFlavor::Film );
}

const struct { const char *name; LookAssistScene scene; } kSceneNames[] = {
    { "Night", LookAssistScene::Night }, { "ArtificialLights", LookAssistScene::ArtificialLights },
    { "Shade", LookAssistScene::Shade }, { "BrightSun", LookAssistScene::BrightSun } };

// Curves::configuration's own formatting of a parsed curve (QPointF holds the toFloat values as doubles).
QString widgetConfiguration( const QString &curve )
{
    std::vector<LookAssistGradationPoint> lines[4];
    lookAssistParseGradationCurve( curve, lines );
    QString config;
    for( int i = 0; i < 4; ++i )
    {
        if( i > 0 ) config.append( QStringLiteral("?") );
        for( const LookAssistGradationPoint &p : lines[i] )
            config.append( QString( "%1;%2;" ).arg( static_cast<double>( p.x ) ).arg( static_cast<double>( p.y ) ) );
    }
    return config;
}

} // namespace

TEST(LookAssistFlavors, TheDocumentedFilmTableIsTheCodeTable)
{
    const QString doc = readSource( QStringLiteral("docs/look-assist-flavors.md") );
    const int begin = doc.indexOf( QStringLiteral("<!-- film-table:begin -->") );
    const int end = doc.indexOf( QStringLiteral("<!-- film-table:end -->") );
    ASSERT_TRUE( begin >= 0 && end > begin );
    const QString block = doc.mid( begin, end - begin );
    int rows = 0;
    for( const auto &s : kSceneNames )
    {
        // | Scene | s | R@0.10 | G@0.10 | B@0.10 | R@0.48 | G@0.48 | B@0.48 | Y@0 | Y@0.80 | Y@1 |
        const QString number = QStringLiteral("\\s*([0-9.]+)\\s*\\|");
        QString pattern = QStringLiteral("^\\|\\s*%1\\s*\\|").arg( QLatin1String( s.name ) );
        for( int column = 0; column < 10; ++column ) pattern += number;
        const QRegularExpression row( pattern + QStringLiteral("\\s*$"), QRegularExpression::MultilineOption );
        const QRegularExpressionMatch m = row.match( block );
        ASSERT_TRUE( m.hasMatch() );
        const LookAssistFilmGrade g = lookAssistFilmGradeForScene( s.scene );
        const double expected[10] = { g.strength,
                                      0.10 - g.tealOffset, 0.10 + g.greenShare * g.tealOffset, 0.10 + g.tealOffset,
                                      0.48 + g.warmOffset, 0.48 + g.greenShare * g.warmOffset, 0.48 - g.warmOffset,
                                      g.blackLift, 0.80 - g.shoulderOffset, 1.0 - g.whiteRoll };
        for( int column = 0; column < 10; ++column )
            ASSERT_TRUE( std::fabs( expected[column] - m.captured( column + 1 ).toDouble() ) < 1e-9 );
        // t = 0.038 s, w = 0.024 s, k = 0.15, L = 0.020 s, h = 0.010 s, c = 0.030 s: the recipe, not just the table.
        ASSERT_TRUE( std::fabs( g.tealOffset - 0.038 * g.strength ) < 1e-12 );
        ASSERT_TRUE( std::fabs( g.warmOffset - 0.024 * g.strength ) < 1e-12 );
        ASSERT_TRUE( std::fabs( g.greenShare - 0.15 ) < 1e-12 );
        ASSERT_TRUE( std::fabs( g.blackLift - 0.020 * g.strength ) < 1e-12 );
        ASSERT_TRUE( std::fabs( g.shoulderOffset - 0.010 * g.strength ) < 1e-12 );
        ASSERT_TRUE( std::fabs( g.whiteRoll - 0.030 * g.strength ) < 1e-12 );
        // The legacy v2 recipe is unchanged: t = 0.035 s, w = 0.045 s, everything else v3's.
        const LookAssistFilmGrade v2 = lookAssistFilmGradeV2ForScene( s.scene );
        ASSERT_TRUE( std::fabs( v2.strength - g.strength ) < 1e-12 );
        ASSERT_TRUE( std::fabs( v2.tealOffset - 0.035 * v2.strength ) < 1e-12 );
        ASSERT_TRUE( std::fabs( v2.warmOffset - 0.045 * v2.strength ) < 1e-12 );
        ASSERT_TRUE( v2.greenShare == g.greenShare && v2.blackLift == g.blackLift && v2.shoulderOffset == g.shoulderOffset
                     && v2.whiteRoll == g.whiteRoll );
        // The legacy v1 recipe is unchanged: a = 0.022 s, b = 0.030 s on the same strength table.
        const LookAssistFilmGradeV1 v1 = lookAssistFilmGradeV1ForScene( s.scene );
        ASSERT_TRUE( std::fabs( v1.strength - g.strength ) < 1e-12 );
        ASSERT_TRUE( std::fabs( v1.shadowOffset - 0.022 * v1.strength ) < 1e-12 );
        ASSERT_TRUE( std::fabs( v1.highlightOffset - 0.030 * v1.strength ) < 1e-12 );
        ++rows;
    }
    ASSERT_EQ( 4, rows );
    ASSERT_TRUE( lookAssistFilmGradeId() == QLatin1String( "film-v3" ) );
}

TEST(LookAssistFlavors, FilmsToneIsCinematicsToneOnTheWholeGrid)
{
    LookAssistStats display;
    display.median = 50.0;
    display.p99 = 120.0;
    const std::vector<LookAssistStats> grid = look_assist_flavor_grid::statsGrid();
    for( const LookAssistStats &s : grid )
        for( int sceneIndex = 0; sceneIndex < 4; ++sceneIndex )
            for( int useDisplay = 0; useDisplay < 2; ++useDisplay )
            {
                const LookAssistScene scene = static_cast<LookAssistScene>( sceneIndex );
                const LookAssistStats *disp = useDisplay ? &display : nullptr;
                const LookAssistPreset cine = cinematic( scene, s, &s, disp );
                const LookAssistPreset f = film( scene, s, &s, disp );
                ASSERT_TRUE( f.exposure == cine.exposure && f.contrast == cine.contrast && f.pivot == cine.pivot
                          && f.shadows == cine.shadows && f.highlights == cine.highlights && f.vibrance == cine.vibrance
                          && f.temperatureDelta == cine.temperatureDelta && f.tintDelta == cine.tintDelta );
                LookAssistPreset overlaid = classicDefault( scene, s, &s, disp );
                lookAssistApplyFlavorDeltas( &overlaid, scene, LookAssistFlavor::Film );
                ASSERT_TRUE( overlaid.contrast == cine.contrast && overlaid.highlights == cine.highlights
                          && overlaid.vibrance == cine.vibrance && overlaid.exposure == cine.exposure );
            }
}

TEST(LookAssistFlavors, FilmIsOneMoreSpellingOfTheSameSelector)
{
    ASSERT_TRUE( lookAssistFlavorName( LookAssistFlavor::Film ) == QLatin1String( "film" ) );
    LookAssistFlavorSelection s = lookAssistSelectFlavor( QStringLiteral("film"), QString(), QString() );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Film && s.source == QLatin1String( "env" ) );
    s = lookAssistSelectFlavor( QString(), QStringLiteral("film"), QString() );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Film && s.source == QLatin1String( "receipt" ) );
    s = lookAssistSelectFlavor( QString(), QString(), QStringLiteral("film") );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Film && s.source == QLatin1String( "app" ) );
    s = lookAssistSelectFlavor( QStringLiteral("  FILM "), QStringLiteral("cinematic"), QString() );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Film && !s.unknownValue );
    s = lookAssistSelectFlavor( QStringLiteral("filmm"), QStringLiteral("film"), QStringLiteral("film") );
    ASSERT_TRUE( s.flavor == LookAssistFlavor::Classic && s.unknownValue && s.rejectedValue == QLatin1String( "filmm" ) );
    LookAssistFlavorSelection sel;
    ASSERT_TRUE( lookAssistSelectorValueForReceipt( QStringLiteral(" Film "), &sel ) == QLatin1String( "film" ) );
    ASSERT_FALSE( sel.unknownValue );
    ASSERT_TRUE( lookAssistSelectorValueForReceipt( QStringLiteral("film grade"), &sel ) == QLatin1String( "classic" ) );
    ASSERT_TRUE( sel.unknownValue );

    // The receipt element round-trips "film" (written: a non-Classic flavor; read back by the loader).
    QTemporaryDir tempDir;
    ASSERT_TRUE( tempDir.isValid() );
    const QString path = tempDir.filePath( QStringLiteral("film.marxml") );
    QFile file( path );
    ASSERT_TRUE( file.open( QIODevice::WriteOnly | QIODevice::Text ) );
    QTextStream out( &file );
    out << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<receipt version=\"4\" mlvapp=\"test\">\n"
        << "  <lookAssistEnabled>1</lookAssistEnabled>\n  <lookAssistFlavor>film</lookAssistFlavor>\n"
        << "  <lookAssistBaselineGradationCurve>1e-05;1e-05;1;1;?1e-05;1e-05;1;1;?1e-05;1e-05;1;1;?1e-05;1e-05;1;1;</lookAssistBaselineGradationCurve>\n"
        << "</receipt>\n";
    file.close();
    ReceiptSettings receipt;
    QString error;
    ASSERT_TRUE( ReceiptLoader::loadFromFile( path, &receipt, &error ) );
    ASSERT_TRUE( receipt.lookAssistFlavor() == QLatin1String( "film" ) );
    ASSERT_TRUE( lookAssistSelectFlavor( QString(), receipt.lookAssistFlavor(), QString() ).flavor == LookAssistFlavor::Film );
    ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve()
                 == QLatin1String( "1e-05;1e-05;1;1;?1e-05;1e-05;1;1;?1e-05;1e-05;1;1;?1e-05;1e-05;1;1;" ) );
    ASSERT_TRUE( ReceiptSettings().lookAssistBaselineGradationCurve().isEmpty() );
}

TEST(LookAssistFlavors, TheFilmCurveStringRoundTripsThroughTheWidgetFormat)
{
    for( const auto &s : kSceneNames )
    {
        const QString curve = lookAssistFilmGradationCurve( s.scene );
        ASSERT_TRUE( widgetConfiguration( curve ) == curve );
        ASSERT_TRUE( widgetConfiguration( widgetConfiguration( curve ) ) == curve );
        std::vector<LookAssistGradationPoint> lines[4];
        ASSERT_EQ( 4, lookAssistParseGradationCurve( curve, lines ) );
        ASSERT_EQ( 4, static_cast<int>( lines[0].size() ) );   // Y: lift, mid-point, shoulder, white roll
        ASSERT_EQ( 7, static_cast<int>( lines[1].size() ) );   // R split
        ASSERT_EQ( 7, static_cast<int>( lines[2].size() ) );   // G share
        ASSERT_EQ( 7, static_cast<int>( lines[3].size() ) );   // B split
        const LookAssistFilmGrade g = lookAssistFilmGradeForScene( s.scene );
        ASSERT_TRUE( std::fabs( lines[0][0].y - g.blackLift ) < 1e-6 );
        ASSERT_TRUE( std::fabs( lines[0][1].y - 0.50 ) < 1e-6 );
        ASSERT_TRUE( std::fabs( lines[0][2].y - ( 0.80 - g.shoulderOffset ) ) < 1e-6 );
        ASSERT_TRUE( std::fabs( lines[0][3].y - ( 1.0 - g.whiteRoll ) ) < 1e-6 );
        ASSERT_TRUE( std::fabs( lines[1][1].y - ( 0.10 - g.tealOffset ) ) < 1e-6 );
        ASSERT_TRUE( std::fabs( lines[2][1].y - ( 0.10 + g.greenShare * g.tealOffset ) ) < 1e-6 );
        ASSERT_TRUE( std::fabs( lines[3][1].y - ( 0.10 + g.tealOffset ) ) < 1e-6 );
        for( int channel = 1; channel <= 3; ++channel )   // the teal zero, the neutral zone's end, the split's end
        {
            ASSERT_TRUE( std::fabs( lines[channel][2].x - 0.20 ) < 1e-6 && std::fabs( lines[channel][2].y - 0.20 ) < 1e-6 );
            ASSERT_TRUE( std::fabs( lines[channel][3].x - 0.28 ) < 1e-6 && std::fabs( lines[channel][3].y - 0.28 ) < 1e-6 );
            ASSERT_TRUE( std::fabs( lines[channel][4].x - 0.48 ) < 1e-6 );
            ASSERT_TRUE( std::fabs( lines[channel][5].x - 0.75 ) < 1e-6 && std::fabs( lines[channel][5].y - 0.75 ) < 1e-6 );
        }
        ASSERT_TRUE( std::fabs( lines[1][4].y - ( 0.48 + g.warmOffset ) ) < 1e-6 );
        ASSERT_TRUE( std::fabs( lines[2][4].y - ( 0.48 + g.greenShare * g.warmOffset ) ) < 1e-6 );
        ASSERT_TRUE( std::fabs( lines[3][4].y - ( 0.48 - g.warmOffset ) ) < 1e-6 );
        ASSERT_FALSE( lookAssistIsDefaultGradationCurve( curve ) );
        // The legacy v2 and v1 curves round-trip too (they are still recognised on old receipts).
        for( const QString &legacy : { lookAssistFilmGradationCurveV2( s.scene ), lookAssistFilmGradationCurveV1( s.scene ) } )
        {
            ASSERT_TRUE( widgetConfiguration( legacy ) == legacy );
            ASSERT_FALSE( lookAssistIsDefaultGradationCurve( legacy ) );
        }
    }
    // The shape the widget writes, exactly (Shade): film-v3, and the legacy v2 and v1 byte for byte as #328 and #319 laid
    // them.
    ASSERT_TRUE( lookAssistFilmGradationCurve( LookAssistScene::Shade ) == QLatin1String(
        "1e-05;0.02;0.5;0.5;0.8;0.79;1;0.97;"
        "?1e-05;1e-05;0.1;0.062;0.2;0.2;0.28;0.28;0.48;0.504;0.75;0.75;1;1;"
        "?1e-05;1e-05;0.1;0.1057;0.2;0.2;0.28;0.28;0.48;0.4836;0.75;0.75;1;1;"
        "?1e-05;1e-05;0.1;0.138;0.2;0.2;0.28;0.28;0.48;0.456;0.75;0.75;1;1;" ) );
    ASSERT_TRUE( lookAssistFilmGradationCurveV2( LookAssistScene::Shade ) == QLatin1String(
        "1e-05;0.02;0.5;0.5;0.8;0.79;1;0.97;"
        "?1e-05;1e-05;0.1;0.065;0.2;0.2;0.26;0.26;0.52;0.565;0.85;0.8635;1;1;"
        "?1e-05;1e-05;0.1;0.10525;0.2;0.2;0.26;0.26;0.52;0.52675;0.85;0.852025;1;1;"
        "?1e-05;1e-05;0.1;0.135;0.2;0.2;0.26;0.26;0.52;0.475;0.85;0.8365;1;1;" ) );
    ASSERT_TRUE( lookAssistFilmGradationCurveV1( LookAssistScene::Shade ) == QLatin1String(
        "1e-05;1e-05;1;1;?1e-05;1e-05;0.18;0.158;0.45;0.45;0.72;0.75;1;1;?1e-05;1e-05;1;1;?1e-05;1e-05;0.18;0.202;0.45;0.45;0.72;0.69;1;1;" ) );
    // Default: the receipt default, the widget's rewrite of it, and empty; not three lines, not a moved point.
    ASSERT_TRUE( lookAssistIsDefaultGradationCurve( ReceiptSettings().gradationCurve() ) );
    ASSERT_TRUE( lookAssistIsDefaultGradationCurve( widgetConfiguration( ReceiptSettings().gradationCurve() ) ) );
    ASSERT_TRUE( lookAssistIsDefaultGradationCurve( QString() ) );
    ASSERT_FALSE( lookAssistIsDefaultGradationCurve( QStringLiteral("1e-5;1e-5;1;1;?1e-5;1e-5;1;1;?1e-5;1e-5;1;1;") ) );
    ASSERT_FALSE( lookAssistIsDefaultGradationCurve( QStringLiteral("1e-5;1e-5;1;0.98;?1e-5;1e-5;1;1;?1e-5;1e-5;1;1;?1e-5;1e-5;1;1;") ) );

    // The parse is the widget's: a line ends at '?', a value at ';', numbers by toFloat.
    const QString curves = readSource( QStringLiteral("platform/qt/Curves.cpp") );
    const int at = curves.indexOf( QStringLiteral("void Curves::setConfiguration(QString config)") );
    ASSERT_TRUE( at >= 0 );
    const QString body = curves.mid( at, 1100 );
    ASSERT_TRUE( body.contains( QStringLiteral("!config.startsWith( \"?\" )") ) );
    ASSERT_TRUE( body.contains( QStringLiteral("config.indexOf( \";\" )") ) );
    ASSERT_TRUE( body.contains( QStringLiteral("val.toFloat()") ) );
    ASSERT_TRUE( curves.contains( QStringLiteral("QString( \"%1;%2;\" ).arg(") ) );
    const QString analysis = readSource( QStringLiteral("src/batch/LookAssistAnalysis.cpp") );
    const int parseAt = analysis.indexOf( QStringLiteral("int lookAssistParseGradationCurve(") );
    ASSERT_TRUE( parseAt >= 0 );
    const QString parse = analysis.mid( parseAt, 1300 );
    ASSERT_TRUE( parse.contains( QStringLiteral("startsWith( QLatin1Char('?') )") ) );
    ASSERT_TRUE( parse.contains( QStringLiteral("indexOf( QLatin1Char(';') )") ) );
    ASSERT_TRUE( parse.contains( QStringLiteral(".toFloat()") ) );
}

TEST(LookAssistFlavors, TheFilmGradeIsLaidOnlyOverADefaultCurve)
{
    const QString user = QStringLiteral("1e-05;1e-05;0.5;0.56;1;1;?1e-05;1e-05;1;1;?1e-05;1e-05;1;1;?1e-05;1e-05;1;1;");
    ASSERT_TRUE( lookAssistFilmGradeDecision( LookAssistFlavor::Film, ReceiptSettings().gradationCurve() ) == QLatin1String( "film-v3" ) );
    ASSERT_TRUE( lookAssistFilmGradeDecision( LookAssistFlavor::Film, QString() ) == QLatin1String( "film-v3" ) );
    ASSERT_TRUE( lookAssistFilmGradeDecision( LookAssistFlavor::Film, user ) == QLatin1String( "skipped_user_curve" ) );
    // A Film curve already on the receipt is not "default": a stale one is never re-laid over itself as the user's. (The
    // consumers put a curve Film owns back to its baseline before they ask, so this is only reached for a bare curve.)
    ASSERT_TRUE( lookAssistFilmGradeDecision( LookAssistFlavor::Film, lookAssistFilmGradationCurve( LookAssistScene::Night ) )
                 == QLatin1String( "skipped_user_curve" ) );
    ASSERT_TRUE( lookAssistFilmGradeDecision( LookAssistFlavor::Film, lookAssistFilmGradationCurveV2( LookAssistScene::Night ) )
                 == QLatin1String( "skipped_user_curve" ) );
    ASSERT_TRUE( lookAssistFilmGradeDecision( LookAssistFlavor::Film, lookAssistFilmGradationCurveV1( LookAssistScene::Night ) )
                 == QLatin1String( "skipped_user_curve" ) );
    ASSERT_TRUE( lookAssistFilmGradeDecision( LookAssistFlavor::Cinematic, ReceiptSettings().gradationCurve() ) == QLatin1String( "none" ) );
    ASSERT_TRUE( lookAssistFilmGradeDecision( LookAssistFlavor::Classic, user ) == QLatin1String( "none" ) );
}

TEST(LookAssistFlavors, LookAssistOffLeavesAFilmSettingUntouchedAndPutsALaidCurveBack)
{
    FlavorEnvGuard guard;
    qputenv( "MLVAPP_LOOK_ASSIST_FLAVOR", "film" );
    // Off, Film selected, nothing laid: the receipt is unchanged (the applier never reaches the grade).
    ReceiptSettings off;
    off.setLookAssistEnabled( false );
    off.setLookAssistFlavor( QStringLiteral("film") );
    const QString curveBefore = off.gradationCurve();
    const int contrastBefore = off.contrast();
    ASSERT_FALSE( ReceiptApplier::applyHeadlessLookAssist( &off, nullptr, nullptr, 0 ) );
    ASSERT_TRUE( off.gradationCurve() == curveBefore );
    ASSERT_TRUE( off.lookAssistBaselineGradationCurve().isEmpty() );
    ASSERT_TRUE( off.lookAssistFlavor() == QLatin1String( "film" ) );
    ASSERT_EQ( contrastBefore, off.contrast() );

    // Off after a Film grade was laid: the curve it replaced comes back and the element goes away.
    ReceiptSettings graded;
    graded.setLookAssistEnabled( false );
    graded.setGradationCurve( lookAssistFilmGradationCurve( LookAssistScene::Shade ) );
    graded.setLookAssistBaselineGradationCurve( ReceiptSettings().gradationCurve() );
    ASSERT_FALSE( ReceiptApplier::applyHeadlessLookAssist( &graded, nullptr, nullptr, 0 ) );
    ASSERT_TRUE( graded.gradationCurve() == ReceiptSettings().gradationCurve() );
    ASSERT_TRUE( graded.lookAssistBaselineGradationCurve().isEmpty() );
}

TEST(LookAssistFlavors, AUserEditOfTheFilmCurveIsKeptAndRetiresFilmsOwnership)
{
    // Film laid over the default curve, then the user adds a Y point (0.5,0.56) to it. The edit is the user's curve now:
    // putting the baseline back (Look Assist off, here; a Classic re-run takes the same helper) keeps the edit and drops
    // the baseline, so Film no longer owns the curve. An untouched Film curve still goes back to the baseline.
    QStringList lines = lookAssistFilmGradationCurve( LookAssistScene::Shade ).split( QLatin1Char('?') );
    ASSERT_EQ( 4, lines.size() );
    lines[0] = QStringLiteral("1e-05;1e-05;0.5;0.56;1;1;");
    const QString edited = lines.join( QLatin1Char('?') );
    const QString base = ReceiptSettings().gradationCurve();
    for( int s = 0; s < 4; ++s )
        ASSERT_TRUE( lookAssistFilmOwnsGradationCurve( lookAssistFilmGradationCurve( static_cast<LookAssistScene>( s ) ) ) );
    ASSERT_FALSE( lookAssistFilmOwnsGradationCurve( edited ) );
    QStringList v1Lines = lookAssistFilmGradationCurveV1( LookAssistScene::Shade ).split( QLatin1Char('?') );
    v1Lines[0] = QStringLiteral("1e-05;1e-05;0.5;0.56;1;1;");
    ASSERT_FALSE( lookAssistFilmOwnsGradationCurve( v1Lines.join( QLatin1Char('?') ) ) );   // an edit of a v1 curve too
    QStringList v2Lines = lookAssistFilmGradationCurveV2( LookAssistScene::Shade ).split( QLatin1Char('?') );
    v2Lines[0] = QStringLiteral("1e-05;1e-05;0.5;0.56;1;1;");
    ASSERT_FALSE( lookAssistFilmOwnsGradationCurve( v2Lines.join( QLatin1Char('?') ) ) );   // and of a v2 curve
    ASSERT_FALSE( lookAssistFilmOwnsGradationCurve( base ) );
    ASSERT_FALSE( lookAssistFilmOwnsGradationCurve( QString() ) );
    ASSERT_TRUE( lookAssistGradationCurveAfterFilmRestore( lookAssistFilmGradationCurve( LookAssistScene::Night ), base ) == base );
    ASSERT_TRUE( lookAssistGradationCurveAfterFilmRestore( edited, base ) == edited );

    FlavorEnvGuard guard;
    qputenv( "MLVAPP_LOOK_ASSIST_FLAVOR", "classic" );
    ReceiptSettings receipt;
    receipt.setLookAssistEnabled( false );
    receipt.setGradationCurve( edited );
    receipt.setLookAssistBaselineGradationCurve( base );
    ASSERT_FALSE( ReceiptApplier::applyHeadlessLookAssist( &receipt, nullptr, nullptr, 0 ) );
    ASSERT_TRUE( receipt.gradationCurve() == edited );
    ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve().isEmpty() );

    // The GUI helper takes the same rule, on the curve the widget shows.
    const QString window = readSource( QStringLiteral("platform/qt/MainWindow.cpp") );
    const int at = window.indexOf( QStringLiteral("void MainWindow::restoreLookAssistBaselineGradationCurve( ReceiptSettings *receipt )") );
    ASSERT_TRUE( at >= 0 );
    ASSERT_TRUE( window.mid( at, 900 ).contains( QStringLiteral("lookAssistGradationCurveAfterFilmRestore( ui->labelCurves->configuration(),") ) );
    const QString applier = readSource( QStringLiteral("src/batch/ReceiptApplier.cpp") );
    const int headlessAt = applier.indexOf( QStringLiteral("static void restoreHeadlessLookAssistGradationCurve( ReceiptSettings *receipt )") );
    ASSERT_TRUE( headlessAt >= 0 );
    ASSERT_TRUE( applier.mid( headlessAt, 600 ).contains( QStringLiteral("lookAssistGradationCurveAfterFilmRestore( receipt->gradationCurve(),") ) );
}

TEST(LookAssistFlavors, AV2LaidCurveIsStillRestoredByLookAssistOff)
{
    // A receipt saved under #328 holds a film-v2 curve and the default as lookAssistBaselineGradationCurve. film-v3
    // replaced v2, but Film still owns every v2 curve, so Look Assist off puts the default back -- headless, and through
    // the GUI's shared helper -- instead of keeping the v2 curve as if the user had drawn it.
    const QString base = ReceiptSettings().gradationCurve();
    for( int s = 0; s < 4; ++s )
    {
        const LookAssistScene scene = static_cast<LookAssistScene>( s );
        const QString v2 = lookAssistFilmGradationCurveV2( scene );
        ASSERT_TRUE( v2 != lookAssistFilmGradationCurve( scene ) );
        ASSERT_TRUE( lookAssistFilmOwnsGradationCurve( v2 ) );
        ASSERT_TRUE( lookAssistGradationCurveAfterFilmRestore( v2, base ) == base );   // the GUI's and headless's helper
    }

    FlavorEnvGuard guard;
    qputenv( "MLVAPP_LOOK_ASSIST_FLAVOR", "film" );
    ReceiptSettings receipt;
    receipt.setLookAssistEnabled( false );
    receipt.setGradationCurve( lookAssistFilmGradationCurveV2( LookAssistScene::Shade ) );
    receipt.setLookAssistBaselineGradationCurve( base );
    ASSERT_FALSE( ReceiptApplier::applyHeadlessLookAssist( &receipt, nullptr, nullptr, 0 ) );
    ASSERT_TRUE( receipt.gradationCurve() == base );
    ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve().isEmpty() );
}

TEST(LookAssistFlavors, AV1LaidCurveIsStillRestoredByLookAssistOff)
{
    // A receipt saved under #319 holds a film-v1 curve and the default as lookAssistBaselineGradationCurve. film-v2 and
    // then film-v3 replaced v1, but Film still owns every v1 curve, so Look Assist off puts the default back --
    // headless, and through the GUI's shared helper -- instead of keeping the v1 curve as if the user had drawn it.
    const QString base = ReceiptSettings().gradationCurve();
    for( int s = 0; s < 4; ++s )
    {
        const LookAssistScene scene = static_cast<LookAssistScene>( s );
        const QString v1 = lookAssistFilmGradationCurveV1( scene );
        ASSERT_TRUE( v1 != lookAssistFilmGradationCurve( scene ) );
        ASSERT_TRUE( lookAssistFilmOwnsGradationCurve( v1 ) );
        ASSERT_TRUE( lookAssistGradationCurveAfterFilmRestore( v1, base ) == base );   // the GUI's and headless's helper
    }

    FlavorEnvGuard guard;
    qputenv( "MLVAPP_LOOK_ASSIST_FLAVOR", "film" );
    ReceiptSettings receipt;
    receipt.setLookAssistEnabled( false );
    receipt.setGradationCurve( lookAssistFilmGradationCurveV1( LookAssistScene::Shade ) );
    receipt.setLookAssistBaselineGradationCurve( base );
    ASSERT_FALSE( ReceiptApplier::applyHeadlessLookAssist( &receipt, nullptr, nullptr, 0 ) );
    ASSERT_TRUE( receipt.gradationCurve() == base );
    ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve().isEmpty() );
}

TEST(LookAssistFlavors, NoGradeIsLoggedAfterTheSyncSafetyFallback)
{
    // LOOK-ASSIST-FILM-GRADE-LOG-AFTER-FALLBACK-1: the sync safety fallback puts the curve back after the grade was laid,
    // so its block must return before look_assist.apply.result (the one line that appends grade=filmGrade) is logged.
    const QString window = readSource( QStringLiteral("platform/qt/MainWindow.cpp") );
    const int fallbackAt = window.indexOf( QStringLiteral("restoreLookAssistSafetyBaseline( receipt, safeChromaSmooth, safeChromaSmoothAuto );") );
    ASSERT_TRUE( fallbackAt >= 0 );
    const int resultAt = window.indexOf( QStringLiteral("QStringLiteral(\"look_assist.apply.result\")"), fallbackAt );
    ASSERT_TRUE( resultAt > fallbackAt );
    const int returnAt = window.indexOf( QStringLiteral("return;"), fallbackAt );
    ASSERT_TRUE( returnAt > fallbackAt && returnAt < resultAt );
    ASSERT_TRUE( window.indexOf( QStringLiteral("filmGradeLogTail"), fallbackAt ) > returnAt );
}

TEST(LookAssistFlavors, TheFilmBaselineElementIsWrittenOnlyWhileSet)
{
    const QString window = readSource( QStringLiteral("platform/qt/MainWindow.cpp") );
    const QString loader = readSource( QStringLiteral("src/batch/ReceiptLoader.cpp") );
    const int at = window.indexOf( QStringLiteral("writeTextElement( \"lookAssistBaselineGradationCurve\"") );
    ASSERT_TRUE( at >= 0 );
    ASSERT_TRUE( window.mid( qMax( 0, at - 120 ), 120 ).contains( QStringLiteral("if( !receipt->lookAssistBaselineGradationCurve().isEmpty() )") ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("writeTextElement( \"lookAssistBaselineGradationCurve\"") ) );
    // Read by both readers; carried with the curve on paste and cleared on reset.
    ASSERT_TRUE( window.contains( QStringLiteral("Rxml->name() == QString( \"lookAssistBaselineGradationCurve\" )") ) );
    ASSERT_TRUE( loader.contains( QStringLiteral("Rxml->name() == QString( \"lookAssistBaselineGradationCurve\" )") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("if( paste && cdui->checkBoxGradationCurve->isChecked() ) receiptTarget->setLookAssistBaselineGradationCurve( receiptSource->lookAssistBaselineGradationCurve() );") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("ACTIVE_RECEIPT->setLookAssistBaselineGradationCurve( receipt->lookAssistBaselineGradationCurve() );") ) );
}

TEST(LookAssistFlavors, TheFilmGradeIsLaidAndPutBackAtEverySite)
{
    const QString window = readSource( QStringLiteral("platform/qt/MainWindow.cpp") );
    const QString applier = readSource( QStringLiteral("src/batch/ReceiptApplier.cpp") );
    const QString ui = readSource( QStringLiteral("platform/qt/MainWindow.ui") );

    // GUI: laid with the tone overlay at both sync sites and at the async apply; put back by the baseline restore, a fresh
    // capture and the safety fallback.
    ASSERT_EQ( 2, window.count( QStringLiteral("filmGrade = applyLookAssistFilmGrade( receipt, scene, flavor );") ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("applyLookAssistFilmGrade( activeReceipt, r.sceneId, r.flavor );") ) );
    ASSERT_TRUE( window.count( QStringLiteral("restoreLookAssistBaselineGradationCurve( receipt );") ) >= 2 );
    ASSERT_EQ( 1, window.count( QStringLiteral("restoreLookAssistBaselineGradationCurve( targetReceipt );") ) );
    const int restoreAt = window.indexOf( QStringLiteral("void MainWindow::restoreLookAssistBaseline( ReceiptSettings *receipt )") );
    const int restoreEnd = window.indexOf( QStringLiteral("\n}"), restoreAt );
    ASSERT_TRUE( window.mid( restoreAt, restoreEnd - restoreAt ).contains( QStringLiteral("restoreLookAssistBaselineGradationCurve( receipt );") ) );
    const int captureAt = window.indexOf( QStringLiteral("void MainWindow::captureLookAssistBaseline( ReceiptSettings *receipt )") );
    ASSERT_TRUE( window.mid( captureAt, 400 ).contains( QStringLiteral("restoreLookAssistBaselineGradationCurve( receipt );") ) );
    // grade= is appended only for Film (Classic / Cinematic lines are byte-identical).
    ASSERT_EQ( 1, window.count( QStringLiteral("flavor == LookAssistFlavor::Film ? QStringLiteral(\" grade=\") + filmGrade : QString();") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("flavor=%30\") + filmGradeLogTail )") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("floor_lifted=%4 flavor=%5\") + dispatchFilmGradeLogTail )") ) );

    // Headless: restored at the top of a pass, by a fresh capture and with Look Assist off; laid after the preset.
    ASSERT_TRUE( applier.count( QStringLiteral("restoreHeadlessLookAssistGradationCurve( receipt );") ) >= 3 );
    ASSERT_EQ( 1, applier.count( QStringLiteral("applyHeadlessLookAssistFilmGrade( receipt, scene, flavor );") ) );
    ASSERT_TRUE( applier.contains( QStringLiteral(".replace( QLatin1Char('\\n'), filmGradeLogTail + QLatin1Char('\\n') )") ) );
    ASSERT_TRUE( applier.contains( QStringLiteral("flavor == LookAssistFlavor::Film ? QStringLiteral(\" grade=\") + filmGrade : QString();") ) );

    // The selector: item 2 is "Film grade" (not "Film": the Profile preset is a different thing) with its tooltip.
    ASSERT_TRUE( ui.contains( QStringLiteral("<string>Film grade</string>") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("setItemData( 2, lookAssistFlavorName( LookAssistFlavor::Film ) );") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("tr( \"Colour-graded look: cool shadows, warm highlights\" ), Qt::ToolTipRole );") ) );
}
