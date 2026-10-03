// LOOK-ASSIST-FLAVORS-1: Classic | Cinematic through the real headless Look Assist, on the tracked fixture clips.
//
//  - Classic (the environment unset, or MLVAPP_LOOK_ASSIST_FLAVOR=classic) reproduces MASTER exactly: the receipt
//    sliders, the applied line (but for the appended flavor field) and the sha256 of the rendered frame equal
//    tests/fixtures/look_assist_flavor_classic_baseline.txt, dumped from an UNCHANGED fork/master b5751928 tree
//    with the same helper (look_assist_flavor_run.h).
//  - Cinematic changes only the documented sliders, by the one table, deterministically, never the white
//    balance, and is always reported.
//  - An unknown environment value is Classic, with a logged warning.
//  - With MLVAPP_FLAVOR_SHEET_DIR set, the ContactSheets test writes raw | classic | cinematic renders of the
//    tracked fixtures for the hub's judges; without it that test does nothing.
#define LOOK_FLAVOR_RUN_HAS_RECEIPT_FLAVOR 1
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "look_assist_flavor_run.h"

#include "../../src/batch/LookAssistAnalysis.h"

#include <QDir>
#include <QFile>
#include <QImage>
#include <QMap>
#include <QPainter>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTextStream>

using namespace lookassist;
using namespace look_flavor_run;

namespace
{

class FlavorEnv
{
public:
    explicit FlavorEnv( const char *value )
        : m_wasSet( qEnvironmentVariableIsSet( "MLVAPP_LOOK_ASSIST_FLAVOR" ) ),
          m_old( qgetenv( "MLVAPP_LOOK_ASSIST_FLAVOR" ) )
    {
        if( value ) qputenv( "MLVAPP_LOOK_ASSIST_FLAVOR", value );
        else qunsetenv( "MLVAPP_LOOK_ASSIST_FLAVOR" );
    }
    ~FlavorEnv()
    {
        if( m_wasSet ) qputenv( "MLVAPP_LOOK_ASSIST_FLAVOR", m_old );
        else qunsetenv( "MLVAPP_LOOK_ASSIST_FLAVOR" );
    }
private:
    bool m_wasSet;
    QByteArray m_old;
};

struct BaselineRow
{
    QString key, receipt, sha256, applied;
};

QStringList readBaselineLines()
{
    QFile file( repo_file_path( QStringLiteral("tests/fixtures/look_assist_flavor_classic_baseline.txt") ) );
    QStringList lines;
    if( !file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return lines;
    QTextStream in( &file );
    while( !in.atEnd() )
    {
        const QString line = in.readLine().trimmed();
        if( !line.isEmpty() ) lines << line;
    }
    return lines;
}

bool baselineRow( const QString &key, BaselineRow *out )
{
    for( const QString &line : readBaselineLines() )
    {
        const QStringList parts = line.split( QStringLiteral(" | ") );
        if( parts.size() != 4 || parts[0] != key ) continue;
        out->key = parts[0];
        out->receipt = parts[1];
        out->sha256 = parts[2];
        out->applied = parts[3];
        return true;
    }
    return false;
}

// "exp=160 contrast=15 ..." -> { exp:160, contrast:15, ... }
QMap<QString, int> receiptFields( const QString &receiptLine )
{
    QMap<QString, int> fields;
    for( const QString &part : receiptLine.split( QLatin1Char(' ') ) )
    {
        const int eq = part.indexOf( QLatin1Char('=') );
        if( eq > 0 ) fields.insert( part.left( eq ), part.mid( eq + 1 ).toInt() );
    }
    return fields;
}

// The applied line as the pinned master (b5751928) wrote it: both appended tails taken off, LOOK-ASSIST-DIAG-LOGGING-1's
// decision trace (which starts at " has_ev100=") and this card's flavor field (always last, after the trace).
QString withoutFlavorField( const QString &appliedLine )
{
    QString s = appliedLine;
    s.remove( QRegularExpression( QStringLiteral(" flavor=\\S+$") ) );
    const int trace = s.indexOf( QStringLiteral(" has_ev100=") );
    if( trace >= 0 ) s.truncate( trace );
    return s;
}

QString appliedField( const QString &appliedLine, const QString &name )
{
    const QRegularExpressionMatch m =
        QRegularExpression( QStringLiteral("(?:^| )%1=(\\S+)").arg( QRegularExpression::escape( name ) ) )
            .match( appliedLine );
    return m.hasMatch() ? m.captured( 1 ) : QString();
}

LookAssistScene sceneByName( const QString &name )
{
    for( int i = 0; i < 4; ++i )
        if( lookAssistSceneName( static_cast<LookAssistScene>( i ) ) == name ) return static_cast<LookAssistScene>( i );
    return LookAssistScene::Night;
}

int clampInt( int lo, int v, int hi ) { return v < lo ? lo : ( v > hi ? hi : v ); }

QImage toImage( const Run &r )
{
    return QImage( r.rgb.data(), r.width, r.height, r.width * 3, QImage::Format_RGB888 ).copy();
}

} // namespace

// Each fixture frame renders through the headless Look Assist (~19 s locally), so each spelling of "Classic" is its own
// test: the hosted runner bounds a shard at 240 s and the three together ran 172 s locally.
void expectClassicIsMaster( const char *mode )
{
    for( const FixtureCase &c : fixtureCases() )
    {
        FlavorEnv env( mode );
        const Run r = run( c, true );
        ASSERT_TRUE( r.ok && r.applied );
        BaselineRow master;
        ASSERT_TRUE( baselineRow( r.key, &master ) );
        ASSERT_TRUE( r.receipt == master.receipt );          // every slider, byte for byte
        ASSERT_TRUE( r.sha256 == master.sha256 );            // the picture, byte for byte
        ASSERT_TRUE( withoutFlavorField( r.appliedLine ) == master.applied );   // the analysis, field for field
        // ... and the flavor is reported, always.
        ASSERT_TRUE( r.appliedLine.endsWith( QStringLiteral(" flavor=classic") ) );
        ASSERT_TRUE( r.flavorOnReceipt == QLatin1String( "classic" ) );
    }
}

// Unset and explicit "classic" are the same run, and both are master's.
TEST(LookAssistFlavorsFixture, ClassicIsMasterForTheTrackedFixturesWhenNothingIsSet)
{
    expectClassicIsMaster( nullptr );
}

TEST(LookAssistFlavorsFixture, ClassicIsMasterForTheTrackedFixturesWhenAskedForExplicitly)
{
    expectClassicIsMaster( "classic" );
}

TEST(LookAssistFlavorsFixture, ClassicIsMasterForTheTrackedFixturesWhateverTheCaseOrSpacing)
{
    expectClassicIsMaster( "  Classic " );
}

TEST(LookAssistFlavorsFixture, CinematicChangesOnlyTheDocumentedSlidersAndIsReported)
{
    FlavorEnv env( "cinematic" );
    QString firstCinematicSha;
    QString firstCinematicReceipt;
    bool first = true;
    for( const FixtureCase &c : fixtureCases() )
    {
        const Run r = run( c, true );
        ASSERT_TRUE( r.ok && r.applied );
        BaselineRow master;
        ASSERT_TRUE( baselineRow( r.key, &master ) );
        const QMap<QString, int> classic = receiptFields( master.receipt );
        const QMap<QString, int> cine = receiptFields( r.receipt );

        // The flavor is reported on the applied line (appended) and on the receipt.
        ASSERT_TRUE( r.appliedLine.endsWith( QStringLiteral(" flavor=cinematic") ) );
        ASSERT_TRUE( r.flavorOnReceipt == QLatin1String( "cinematic" ) );

        // Same analysis, same scene verdict, same white-balance decision: the applied line is master's up to the
        // slider fields the flavor owns (exposure is held, so even that field matches).
        const QString scene = appliedField( r.appliedLine, QStringLiteral("scene") );
        ASSERT_TRUE( scene == appliedField( master.applied, QStringLiteral("scene") ) );
        ASSERT_TRUE( appliedField( r.appliedLine, QStringLiteral("temperature") ) == appliedField( master.applied, QStringLiteral("temperature") ) );
        ASSERT_TRUE( appliedField( r.appliedLine, QStringLiteral("tint") ) == appliedField( master.applied, QStringLiteral("tint") ) );
        ASSERT_TRUE( withoutFlavorField( r.appliedLine ) == master.applied );
        ASSERT_EQ( classic.value( QStringLiteral("temp") ), cine.value( QStringLiteral("temp") ) );
        ASSERT_EQ( classic.value( QStringLiteral("tint") ), cine.value( QStringLiteral("tint") ) );
        ASSERT_EQ( classic.value( QStringLiteral("chromaSmooth") ), cine.value( QStringLiteral("chromaSmooth") ) );

        // The six sliders move by exactly the documented table.
        const LookAssistFlavorDeltas d = lookAssistCinematicDeltasForScene( sceneByName( scene ) );
        ASSERT_EQ( clampInt( -180, classic.value( QStringLiteral("exp") ) + d.exposure, 380 ), cine.value( QStringLiteral("exp") ) );
        ASSERT_EQ( clampInt( -100, classic.value( QStringLiteral("contrast") ) + d.contrast, 100 ), cine.value( QStringLiteral("contrast") ) );
        ASSERT_EQ( clampInt( 0, classic.value( QStringLiteral("pivot") ) + d.pivot, 100 ), cine.value( QStringLiteral("pivot") ) );
        ASSERT_EQ( clampInt( -100, classic.value( QStringLiteral("shadows") ) + d.shadows, 100 ), cine.value( QStringLiteral("shadows") ) );
        ASSERT_EQ( clampInt( -100, classic.value( QStringLiteral("highlights") ) + d.highlights, 100 ), cine.value( QStringLiteral("highlights") ) );
        ASSERT_EQ( clampInt( -100, classic.value( QStringLiteral("vibrance") ) + d.vibrance, 100 ), cine.value( QStringLiteral("vibrance") ) );

        // The headless (CDNG) picture is white balance and raw fixes only, and the flavor does not touch either:
        // it is master's, byte for byte. The flavor shows where the sliders are applied (GUI, video export): see
        // CinematicIsADifferentGradedPicture.
        ASSERT_TRUE( r.sha256 == master.sha256 );

        if( first )
        {
            firstCinematicSha = r.sha256;
            firstCinematicReceipt = r.receipt;
            first = false;
        }
    }

    // Deterministic: the same clip, the same frame, the same picture and sliders.
    const Run again = run( fixtureCases().front(), true );
    ASSERT_TRUE( again.ok );
    ASSERT_TRUE( again.sha256 == firstCinematicSha );
    ASSERT_TRUE( again.receipt == firstCinematicReceipt );
}

TEST(LookAssistFlavorsFixture, CinematicIsADifferentGradedPicture)
{
    // With the sliders applied the way the app applies them, Cinematic is a different, deterministic picture, and
    // neither flavor is the unprocessed one.
    const FixtureCase &c = fixtureCases().front();
    const Run raw = run( c, false, true );
    Run classic, cine, cineAgain;
    {
        FlavorEnv env( "classic" );
        classic = run( c, true, true );
    }
    {
        FlavorEnv env( "cinematic" );
        cine = run( c, true, true );
        cineAgain = run( c, true, true );
    }
    ASSERT_TRUE( raw.ok && classic.ok && cine.ok && cineAgain.ok );
    ASSERT_TRUE( cine.sha256 != classic.sha256 );
    ASSERT_TRUE( classic.sha256 != raw.sha256 );
    ASSERT_TRUE( cine.sha256 != raw.sha256 );
    ASSERT_TRUE( cine.sha256 == cineAgain.sha256 );
}

TEST(LookAssistFlavorsFixture, AnUnknownEnvironmentValueIsClassicWithAWarning)
{
    FlavorEnv env( "sepia" );
    const FixtureCase &c = fixtureCases().front();
    const Run r = run( c, true );
    ASSERT_TRUE( r.ok && r.applied );
    BaselineRow master;
    ASSERT_TRUE( baselineRow( r.key, &master ) );
    ASSERT_TRUE( r.receipt == master.receipt );
    ASSERT_TRUE( r.sha256 == master.sha256 );
    ASSERT_TRUE( r.appliedLine.endsWith( QStringLiteral(" flavor=classic") ) );
    ASSERT_TRUE( r.log.contains( "WARNING LOOK_ASSIST unknown flavor 'sepia' from env; using classic" ) );
}

TEST(LookAssistFlavorsFixture, TheReceiptElementIsALayerBelowTheEnvironment)
{
    // The receipt says cinematic, the environment says nothing: cinematic. The environment says classic: classic.
    const FixtureCase &c = fixtureCases().front();
    BaselineRow master;
    ASSERT_TRUE( baselineRow( QStringLiteral("tiny_dual_iso.mlv 0"), &master ) );
    for( int envSaysClassic = 0; envSaysClassic < 2; ++envSaysClassic )
    {
        FlavorEnv env( envSaysClassic ? "classic" : nullptr );
        MlvPipelineFixture fixture;
        QString error_message;
        ASSERT_TRUE( fixture.openClipFile( repo_file_path( QString::fromLatin1( c.file ) ), &error_message ) );
        ASSERT_TRUE( fixture.applyReceipt( &error_message ) );
        ReceiptSettings &receipt = fixture.receipt();
        receipt.setLookAssistEnabled( true );
        receipt.setLookAssistBaselineValid( false );
        receipt.setExposure( 0 );
        receipt.setTemperature( -1 );
        receipt.setTint( 0 );
        receipt.setLookAssistFlavor( QStringLiteral("cinematic") );
        ASSERT_TRUE( ReceiptApplier::applyHeadlessLookAssist( &receipt, fixture.video(), fixture.processing(), 0 ) );
        const QMap<QString, int> classic = receiptFields( master.receipt );
        if( envSaysClassic )
        {
            ASSERT_TRUE( receipt.lookAssistFlavor() == QLatin1String( "classic" ) );
            ASSERT_EQ( classic.value( QStringLiteral("contrast") ), receipt.contrast() );
        }
        else
        {
            ASSERT_TRUE( receipt.lookAssistFlavor() == QLatin1String( "cinematic" ) );
            ASSERT_TRUE( receipt.contrast() != classic.value( QStringLiteral("contrast") ) );
        }
    }
}

// Writes raw | classic | cinematic renders of the tracked fixtures for the hub's model judges. Does nothing unless
// MLVAPP_FLAVOR_SHEET_DIR names a directory. Fixture renders only.
TEST(LookAssistFlavorsFixture, ContactSheets)
{
    const QString dirPath = QString::fromLocal8Bit( qgetenv( "MLVAPP_FLAVOR_SHEET_DIR" ) );
    if( dirPath.isEmpty() ) return;
    QDir dir( dirPath );
    ASSERT_TRUE( dir.mkpath( QStringLiteral(".") ) );
    QStringList manifest;
    for( const FixtureCase &c : fixtureCases() )
    {
        const Run raw = run( c, false, true );
        Run classic, cinematicRun;
        {
            FlavorEnv env( "classic" );
            classic = run( c, true, true );
        }
        {
            FlavorEnv env( "cinematic" );
            cinematicRun = run( c, true, true );
        }
        ASSERT_TRUE( raw.ok && classic.ok && cinematicRun.ok );
        const QString stem = QStringLiteral("%1").arg( raw.key ).replace( QLatin1Char(' '), QLatin1Char('_') ).replace( QStringLiteral(".mlv"), QString() );
        toImage( raw ).save( dir.filePath( stem + QStringLiteral("_raw.png") ) );
        toImage( classic ).save( dir.filePath( stem + QStringLiteral("_classic.png") ) );
        toImage( cinematicRun ).save( dir.filePath( stem + QStringLiteral("_cinematic.png") ) );

        // The sheet: raw | classic | cinematic, side by side, 540 px tall.
        const int panelH = 540;
        QImage panels[3] = { toImage( raw ), toImage( classic ), toImage( cinematicRun ) };
        int totalW = 0;
        for( QImage &p : panels )
        {
            p = p.scaledToHeight( panelH, Qt::SmoothTransformation );
            totalW += p.width();
        }
        QImage sheet( totalW + 8, panelH, QImage::Format_RGB888 );
        sheet.fill( Qt::black );
        QPainter painter( &sheet );
        int x = 0;
        for( QImage &p : panels )
        {
            painter.drawImage( x, 0, p );
            x += p.width() + 4;
        }
        painter.end();
        ASSERT_TRUE( sheet.save( dir.filePath( stem + QStringLiteral("_sheet_raw_classic_cinematic.png") ) ) );
        manifest << QStringLiteral("%1 | raw: %2 | classic: %3 | cinematic: %4")
                        .arg( raw.key, raw.receipt, classic.receipt, cinematicRun.receipt );
        manifest << QStringLiteral("  classic applied: %1").arg( classic.appliedLine.left( 160 ) );
        manifest << QStringLiteral("  cinematic applied: %1").arg( cinematicRun.appliedLine.left( 160 ) );
        manifest << QStringLiteral("  sha256 classic %1 cinematic %2").arg( classic.sha256, cinematicRun.sha256 );
    }
    QFile out( dir.filePath( QStringLiteral("MANIFEST.txt") ) );
    ASSERT_TRUE( out.open( QIODevice::WriteOnly | QIODevice::Text ) );
    QTextStream stream( &out );
    stream << manifest.join( QLatin1Char('\n') ) << "\n";
}
