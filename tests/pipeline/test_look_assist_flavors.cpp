// LOOK-ASSIST-FLAVORS-1: Classic | Cinematic through the real headless Look Assist, on the tracked fixture clips.
//
//  - Classic (the environment unset, or MLVAPP_LOOK_ASSIST_FLAVOR=classic) reproduces MASTER exactly: the receipt
//    sliders, the applied line (but for the appended flavor field) and the sha256 of the rendered frame equal
//    tests/fixtures/look_assist_flavor_classic_baseline.txt, dumped from an UNCHANGED fork/master b5751928 tree
//    with the same helper (look_assist_flavor_run.h). LOOK-ASSIST-DISPLAY-METER-ALL-SCALES-1 deliberately moves
//    headless Classic's exposure onto the shared display meter, so its three rows were re-dumped with that same
//    helper from this tree (Classic, flavor unset); the console grid pin (master's preset function) is untouched.
//    LOOK-ASSIST-ANALYSIS-TRUE-LEVELS-1 (#259) moves only the exposure fields again (13 -> 154, 16 -> 163, 16 -> 163):
//    the meter reads the display's levels (MLV_PROCESSED_THUMBNAIL_DISPLAY_LEVELS). With that flag off the tree
//    reproduces the previous three rows byte for byte; the rows are re-dumped from the FLAVOR-BASELINE lines below.
//    LOOK-ASSIST-DUALISO-VSTRIPES-1 re-dumps all three rows, in file order, from c7e51d03 with vertical stripes forced off:
//      row 1 was exp=154 temp=6540 tint=-35, 81b7eafc...; stripes no longer run on dual-ISO frames.
//      row 2 was exp=163 temp=6540 tint=-35, 81b7eafc...; stripes no longer run on dual-ISO frames.
//      row 3 was shade exp=163 temp=6310 tint=-21, d7544133... (now night); stripes no longer run on dual-ISO frames.
//  - Cinematic changes only the documented sliders, by the one table, deterministically, never the white
//    balance, and is always reported.
//  - An unknown environment value is Classic, with a logged warning.
//  - With MLVAPP_FLAVOR_SHEET_DIR set, the ContactSheets test writes raw | classic | cinematic renders of the
//    tracked fixtures for the hub's judges; without it that test does nothing.
#define LOOK_FLAVOR_RUN_HAS_RECEIPT_FLAVOR 1
#define LOOK_FLAVOR_RUN_HAS_FILM_GRADE 1
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "look_assist_flavor_run.h"
#include "look_assist_gradation_curve.h"

#include "../../src/batch/LookAssistAnalysis.h"

#include <QBuffer>
#include <QCryptographicHash>
#include <QDir>
#include <QXmlStreamWriter>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
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
        // The row this run would pin, in the baseline file's own format (how the rows are re-dumped).
        std::fprintf( stderr, "FLAVOR-BASELINE %s | %s | %s | %s\n", qPrintable( r.key ), qPrintable( r.receipt ),
                      qPrintable( r.sha256 ), qPrintable( withoutFlavorField( r.appliedLine ) ) );
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

        // Exposure is Classic's exactly; the five tone sliders move by exactly the documented table.
        const LookAssistFlavorDeltas d = lookAssistCinematicDeltasForScene( sceneByName( scene ) );
        ASSERT_EQ( classic.value( QStringLiteral("exp") ), cine.value( QStringLiteral("exp") ) );
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

// ---- LOOK-ASSIST-FILM-FLAVOR-1: the Film grade (Cinematic's tone + a blue-amber split in the R and B curves) ----

namespace
{

struct GradationTables
{
    std::vector<uint16_t> y, r, g, b;
};

// The engine's four tables for a receipt curve string, built the way the Curves widget builds them.
GradationTables gradationTables( const QString &curve )
{
    GradationTables t;
    processingObject_t *processing = initProcessingObject();
    if( !processing ) return t;
    lookAssistTestApplyGradationCurve( processing, curve );
    t.y.assign( processing->gcurve_y, processing->gcurve_y + 65536 );
    t.r.assign( processing->gcurve_r, processing->gcurve_r + 65536 );
    t.g.assign( processing->gcurve_g, processing->gcurve_g + 65536 );
    t.b.assign( processing->gcurve_b, processing->gcurve_b + 65536 );
    freeProcessingObject( processing );
    return t;
}

QString tablesSha256( const GradationTables &t )
{
    QCryptographicHash hash( QCryptographicHash::Sha256 );
    for( const std::vector<uint16_t> *table : { &t.y, &t.r, &t.g, &t.b } )
        hash.addData( QByteArray( reinterpret_cast<const char *>( table->data() ), static_cast<int>( table->size() * sizeof( uint16_t ) ) ) );
    return QString::fromLatin1( hash.result().toHex() );
}

int tableIndex( double x ) { return static_cast<int>( x * 65535.0 + 0.5 ); }

// The green-magenta axis G - (R+B)/2 of one display-referred pixel after the engine's gradation stage, in 8-bit code
// values (16-bit / 257). The stage as every kernel applies it: each channel through Y, then through its own table.
double gradedGreenAxis8( const GradationTables &t, double r, double g, double b )
{
    const double R = t.r[t.y[tableIndex( r )]];
    const double G = t.g[t.y[tableIndex( g )]];
    const double B = t.b[t.y[tableIndex( b )]];
    return ( G - ( R + B ) / 2.0 ) / 257.0;
}

const LookAssistScene kFilmScenes[] = { LookAssistScene::Night, LookAssistScene::ArtificialLights,
                                        LookAssistScene::Shade, LookAssistScene::BrightSun };

QString defaultCurve() { return ReceiptSettings().gradationCurve(); }

// The receipt elements the Look Assist path can touch, serialised with the GUI writer's rules (writeXmlElementsToFile:
// "%1" numbers, lookAssistFlavor only when non-empty and not "classic", lookAssistBaselineGradationCurve only when
// non-empty). MainWindow is not linked into this binary; LookAssistFlavors.TheFilmBaselineElementIsWrittenOnlyWhileSet
// pins the writer's two guards, so these bytes are the writer's bytes for these elements.
QByteArray lookAssistReceiptXml( ReceiptSettings &r )
{
    QByteArray bytes;
    QBuffer buffer( &bytes );
    buffer.open( QIODevice::WriteOnly );
    QXmlStreamWriter xml( &buffer );
    xml.setAutoFormatting( true );
    xml.writeStartDocument();
    xml.writeStartElement( QStringLiteral("receipt") );
    xml.writeTextElement( "exposure", QString( "%1" ).arg( r.exposure() ) );
    xml.writeTextElement( "contrast", QString( "%1" ).arg( r.contrast() ) );
    xml.writeTextElement( "pivot", QString( "%1" ).arg( r.pivot() ) );
    xml.writeTextElement( "temperature", QString( "%1" ).arg( r.temperature() ) );
    xml.writeTextElement( "tint", QString( "%1" ).arg( r.tint() ) );
    xml.writeTextElement( "vibrance", QString( "%1" ).arg( r.vibrance() ) );
    xml.writeTextElement( "shadows", QString( "%1" ).arg( r.shadows() ) );
    xml.writeTextElement( "highlights", QString( "%1" ).arg( r.highlights() ) );
    xml.writeTextElement( "gradationCurve", QString( "%1" ).arg( r.gradationCurve() ) );
    xml.writeTextElement( "lookAssistEnabled", QString( "%1" ).arg( r.lookAssistEnabled() ) );
    if( !r.lookAssistFlavor().isEmpty() && r.lookAssistFlavor() != QLatin1String( "classic" ) )
        xml.writeTextElement( "lookAssistFlavor", r.lookAssistFlavor() );
    xml.writeTextElement( "lookAssistBaselineValid", QString( "%1" ).arg( r.lookAssistBaselineValid() ) );
    xml.writeTextElement( "lookAssistBaselineExposure", QString( "%1" ).arg( r.lookAssistBaselineExposure() ) );
    xml.writeTextElement( "lookAssistBaselineContrast", QString( "%1" ).arg( r.lookAssistBaselineContrast() ) );
    xml.writeTextElement( "lookAssistBaselinePivot", QString( "%1" ).arg( r.lookAssistBaselinePivot() ) );
    xml.writeTextElement( "lookAssistBaselineTemperature", QString( "%1" ).arg( r.lookAssistBaselineTemperature() ) );
    xml.writeTextElement( "lookAssistBaselineTint", QString( "%1" ).arg( r.lookAssistBaselineTint() ) );
    xml.writeTextElement( "lookAssistBaselineVibrance", QString( "%1" ).arg( r.lookAssistBaselineVibrance() ) );
    xml.writeTextElement( "lookAssistBaselineShadows", QString( "%1" ).arg( r.lookAssistBaselineShadows() ) );
    xml.writeTextElement( "lookAssistBaselineHighlights", QString( "%1" ).arg( r.lookAssistBaselineHighlights() ) );
    xml.writeTextElement( "lookAssistBaselineRawBlack", QString( "%1" ).arg( r.lookAssistBaselineRawBlack() ) );
    xml.writeTextElement( "lookAssistBaselineRawWhite", QString( "%1" ).arg( r.lookAssistBaselineRawWhite() ) );
    xml.writeTextElement( "lookAssistBaselineChromaSmooth", QString( "%1" ).arg( r.lookAssistBaselineChromaSmooth() ) );
    if( !r.lookAssistBaselineGradationCurve().isEmpty() )
        xml.writeTextElement( "lookAssistBaselineGradationCurve", r.lookAssistBaselineGradationCurve() );
    xml.writeTextElement( "chromaSmooth", QString( "%1" ).arg( r.chromaSmooth() ) );
    xml.writeTextElement( "rawBlack", QString( "%1" ).arg( r.rawBlack() ) );
    xml.writeTextElement( "rawWhite", QString( "%1" ).arg( r.rawWhite() ) );
    xml.writeTextElement( "lookAssistBaselineStretchX", QString( "%1" ).arg( r.lookAssistBaselineStretchX() ) );
    xml.writeTextElement( "lookAssistBaselineStretchY", QString( "%1" ).arg( r.lookAssistBaselineStretchY() ) );
    xml.writeEndElement();
    xml.writeEndDocument();
    return bytes;
}

// One headless Look Assist pass on the fixture's own receipt, capturing the applied line.
bool applyCapturingLine( MlvPipelineFixture &fixture, ReceiptSettings &receipt, QString *appliedLine )
{
    QTemporaryDir temporary_dir;
    const QString log_path = temporary_dir.filePath( QStringLiteral("look_assist.log") );
    BatchLogger::init( log_path );
    const bool applied = ReceiptApplier::applyHeadlessLookAssist( &receipt, fixture.video(), fixture.processing(), 0 );
    BatchLogger::shutdown();
    QFile log_file( log_path );
    if( appliedLine ) appliedLine->clear();
    if( appliedLine && log_file.open( QIODevice::ReadOnly | QIODevice::Text ) )
        for( const QString &line : QString::fromUtf8( log_file.readAll() ).split( QLatin1Char('\n') ) )
            if( line.contains( QLatin1String( "LOOK_ASSIST applied " ) ) ) *appliedLine = line.trimmed();
    return applied;
}

bool openFirstFixture( MlvPipelineFixture &fixture )
{
    QString error_message;
    if( !fixture.openClipFile( repo_file_path( QString::fromLatin1( fixtureCases().front().file ) ), &error_message ) ) return false;
    if( !fixture.applyReceipt( &error_message ) ) return false;
    ReceiptSettings &receipt = fixture.receipt();
    receipt.setLookAssistEnabled( true );
    receipt.setLookAssistBaselineValid( false );
    receipt.setExposure( 0 );
    receipt.setTemperature( -1 );
    receipt.setTint( 0 );
    return true;
}

} // namespace

TEST(LookAssistFilmGrade, FilmCurveIsBlueAmberOnly)
{
    // Y and G are exactly the default receipt's tables; R and B move by equal and opposite amounts, so r + b == 2g on every
    // table entry (the spline is linear in y; only rounding is left). That is a property of the tables at equal indices:
    // per pixel it keeps the green-magenta axis G - (R+B)/2 only on a neutral pixel. A coloured pixel indexes different
    // entries per channel and its axis moves (FilmMovesTheGreenAxisOnlyOnColouredPixelsWithinTheDocumentedBound).
    const GradationTables def = gradationTables( defaultCurve() );
    ASSERT_EQ( 65536, static_cast<int>( def.g.size() ) );
    for( LookAssistScene scene : kFilmScenes )
    {
        const GradationTables film = gradationTables( lookAssistFilmGradationCurve( scene ) );
        ASSERT_TRUE( film.y == def.y );
        ASSERT_TRUE( film.g == def.g );
        int worst = 0;
        for( int v = 0; v < 65536; ++v )
        {
            const int deviation = std::abs( static_cast<int>( film.r[v] ) + static_cast<int>( film.b[v] ) - 2 * static_cast<int>( film.g[v] ) );
            if( deviation > worst ) worst = deviation;
        }
        std::fprintf( stderr, "FILM-CURVE scene=%s max|r+b-2g|=%d\n", qPrintable( lookAssistSceneName( scene ) ), worst );
        ASSERT_TRUE( worst <= 2 );
    }
}

TEST(LookAssistFilmGrade, FilmMovesTheGreenAxisOnlyOnColouredPixelsWithinTheDocumentedBound)
{
    // Per pixel, through the engine's own tables at each Film strength: a neutral pixel keeps G - (R+B)/2 (all three
    // channels index the same entry); a coloured pixel indexes different R and B entries, so its axis moves by
    // -(dR(R) - dR(B))/2: (a + b)/2 in 8-bit code values on the knots, within 2% of that on any pixel, a warm-highlight /
    // cool-shadow (amber) pixel toward magenta, its mirror (teal) toward green. The card's no-magenta tolerance is judged on the venue picture mean, not per
    // saturated pixel (docs/look-assist-flavors.md, "What it does to the green-magenta axis").
    const GradationTables def = gradationTables( defaultCurve() );
    struct Coloured { const char *name; double r, g, b; int sign; };
    const Coloured coloured[] = {
        { "amber", 0.72, 0.45, 0.18, -1 },   // sol r3's repro: R at the highlight knot, B at the shadow knot
        { "teal", 0.18, 0.45, 0.72, +1 },    // its mirror
        { "skin", 0.80, 0.55, 0.35, -1 },
        { "sky", 0.30, 0.55, 0.85, +1 },
        { "sodium", 0.90, 0.50, 0.10, -1 },
        { "cyan", 0.10, 0.50, 0.90, +1 },
    };
    for( LookAssistScene scene : kFilmScenes )
    {
        const LookAssistFilmGrade grade = lookAssistFilmGradeForScene( scene );
        const GradationTables film = gradationTables( lookAssistFilmGradationCurve( scene ) );
        // The documented bounds, plus the tables' own rounding (|r + b - 2g| <= 2 per entry, one 16-bit step per channel):
        // (a + b)/2 for a pixel whose R and B sit within the knots' reach, and 2% more for any pixel at all, because the
        // natural spline dips about 3% past the 0.18 knot between knots (measured: Night peak dR -745 against a = 721).
        const double knots = ( grade.shadowOffset + grade.highlightOffset ) / 2.0 * 255.0;
        const double bound = knots + 2.0 / 257.0;
        const double anyPixelBound = knots * 1.02 + 2.0 / 257.0;

        // (a) Neutral: every grey level, dGA == 0 within the tables' rounding.
        double worstNeutral = 0.0;
        for( int v = 0; v < 65536; ++v )
        {
            const double x = v / 65535.0;
            const double dGA = gradedGreenAxis8( film, x, x, x ) - gradedGreenAxis8( def, x, x, x );
            worstNeutral = std::max( worstNeutral, std::abs( dGA ) );
        }

        // The whole table: the largest R offset up and down, so a bound holds for every input, not only the set below.
        int peakUp = 0, peakDown = 0;
        for( int v = 0; v < 65536; ++v )
        {
            const int dR = static_cast<int>( film.r[v] ) - static_cast<int>( def.r[v] );
            peakUp = std::max( peakUp, dR );
            peakDown = std::min( peakDown, dR );
        }
        const double worstAnyPixel = ( peakUp - peakDown ) / 2.0 / 257.0;

        std::fprintf( stderr, "FILM-GREEN-AXIS scene=%s s=%.2f bound=%.4f any_pixel_bound=%.4f neutral_max|dGA|=%.4f"
                      " any_pixel_max|dGA|=%.4f peak_dR=+%d/%d (a=%d b=%d)\n", qPrintable( lookAssistSceneName( scene ) ),
                      grade.strength, bound, anyPixelBound, worstNeutral, worstAnyPixel, peakUp, peakDown,
                      static_cast<int>( grade.shadowOffset * 65535.0 + 0.5 ), static_cast<int>( grade.highlightOffset * 65535.0 + 0.5 ) );
        ASSERT_TRUE( worstNeutral <= 1.0 / 257.0 + 1e-9 );
        ASSERT_TRUE( worstAnyPixel <= anyPixelBound );

        // (b) Coloured: the documented bound and the documented direction.
        for( const Coloured &c : coloured )
        {
            const double dGA = gradedGreenAxis8( film, c.r, c.g, c.b ) - gradedGreenAxis8( def, c.r, c.g, c.b );
            std::fprintf( stderr, "FILM-GREEN-AXIS scene=%s input=%s rgb=%.2f/%.2f/%.2f dGA=%+.4f bound=%.4f\n",
                          qPrintable( lookAssistSceneName( scene ) ), c.name, c.r, c.g, c.b, dGA, bound );
            ASSERT_TRUE( std::abs( dGA ) <= bound );
            ASSERT_TRUE( dGA * c.sign > 0.0 );
        }

        // The bound is reached, not slack: amber sits on both knots, so it moves by (a + b)/2 to within rounding.
        const double amber = gradedGreenAxis8( film, 0.72, 0.45, 0.18 ) - gradedGreenAxis8( def, 0.72, 0.45, 0.18 );
        ASSERT_NEAR( -( grade.shadowOffset + grade.highlightOffset ) / 2.0 * 255.0, amber, 2.0 / 257.0 );
    }
}

TEST(LookAssistFilmGrade, FilmCurveIsPinned)
{
    // The four 65536-entry tables per scene by sha256, and sampled values, as the engine built them at this commit.
    struct Pin { LookAssistScene scene; const char *sha256; int r[5]; int g[5]; int b[5]; };
    const Pin pins[] = {
        { LookAssistScene::Night, "8f3fc3f0939a4457168298d264076410733d90276c72a2b29dbf4b11caf8518a",
          { 6064, 11074, 29490, 48167, 59495 }, { 6553, 11795, 29490, 47184, 58981 }, { 7043, 12516, 29490, 46201, 58466 } },
        { LookAssistScene::ArtificialLights, "1aaf1f775dca41a4f025a2db2cb55e71b1876156cd104f566484787c09066cac",
          { 5819, 10714, 29490, 48658, 59752 }, { 6553, 11795, 29490, 47184, 58981 }, { 7288, 12877, 29490, 45709, 58209 } },
        { LookAssistScene::Shade, "c247cc0504315fb524e0e5b09823f9096fc6fdcb1e1cebb74e24d160ec78fb4f",
          { 5574, 10354, 29490, 49150, 60010 }, { 6553, 11795, 29490, 47184, 58981 }, { 7533, 13237, 29490, 45218, 57951 } },
        { LookAssistScene::BrightSun, "33ecb6beea2ad3e70d07c1a024a4e502eadf18709e3eb9d2bf8a51be46811724",
          { 5672, 10498, 29490, 48953, 59907 }, { 6553, 11795, 29490, 47184, 58981 }, { 7435, 13093, 29490, 45414, 58054 } },
    };
    const double xs[5] = { 0.10, 0.18, 0.45, 0.72, 0.90 };
    for( const Pin &pin : pins )   // every scene's line first, so a re-pin reads all four from one run
    {
        const GradationTables film = gradationTables( lookAssistFilmGradationCurve( pin.scene ) );
        QString samples;
        for( int i = 0; i < 5; ++i )
        {
            const int v = tableIndex( xs[i] );
            samples += QStringLiteral(" x=%1 r=%2 g=%3 b=%4").arg( xs[i] ).arg( film.r[v] ).arg( film.g[v] ).arg( film.b[v] );
        }
        std::fprintf( stderr, "FILM-CURVE-PIN scene=%s sha256=%s%s\n", qPrintable( lookAssistSceneName( pin.scene ) ),
                      qPrintable( tablesSha256( film ) ), qPrintable( samples ) );
    }
    for( const Pin &pin : pins )
    {
        const GradationTables film = gradationTables( lookAssistFilmGradationCurve( pin.scene ) );
        const QString sha = tablesSha256( film );
        ASSERT_TRUE( sha == QLatin1String( pin.sha256 ) );
        for( int i = 0; i < 5; ++i )
        {
            const int v = tableIndex( xs[i] );
            ASSERT_EQ( pin.r[i], static_cast<int>( film.r[v] ) );
            ASSERT_EQ( pin.g[i], static_cast<int>( film.g[v] ) );
            ASSERT_EQ( pin.b[i], static_cast<int>( film.b[v] ) );
        }
    }
}

TEST(LookAssistFilmGrade, FilmIsAGradeNotATone)
{
    // The inert kill: Shade's split is far outside rounding at both knots, the right way round.
    const GradationTables film = gradationTables( lookAssistFilmGradationCurve( LookAssistScene::Shade ) );
    const int lo = tableIndex( 0.18 );
    const int hi = tableIndex( 0.72 );
    ASSERT_TRUE( static_cast<int>( film.r[lo] ) <= static_cast<int>( film.g[lo] ) - 1000 );
    ASSERT_TRUE( static_cast<int>( film.b[lo] ) >= static_cast<int>( film.g[lo] ) + 1000 );
    ASSERT_TRUE( static_cast<int>( film.r[hi] ) >= static_cast<int>( film.g[hi] ) + 1400 );
    ASSERT_TRUE( static_cast<int>( film.b[hi] ) <= static_cast<int>( film.g[hi] ) - 1400 );
    ASSERT_TRUE( lookAssistFilmGradationCurve( LookAssistScene::Shade ) != defaultCurve() );
    ASSERT_FALSE( lookAssistIsDefaultGradationCurve( lookAssistFilmGradationCurve( LookAssistScene::Shade ) ) );
}

TEST(LookAssistFilmGrade, FilmKeepsTheBalanceAndCinematicsToneOnTheFixtures)
{
    // Same analysis, same scene verdict, same white balance and exposure as Classic (master's rows); the five tone
    // sliders are Cinematic's (Classic + the one table); the receipt carries the scene's Film curve; the headless (CDNG)
    // picture is master's byte for byte (headless never pushes the curve); reported on the applied line and receipt.
    FlavorEnv env( "film" );
    for( const FixtureCase &c : fixtureCases() )
    {
        const Run r = run( c, true );
        ASSERT_TRUE( r.ok && r.applied );
        BaselineRow master;
        ASSERT_TRUE( baselineRow( r.key, &master ) );
        const QMap<QString, int> classic = receiptFields( master.receipt );
        const QMap<QString, int> film = receiptFields( r.receipt );
        ASSERT_TRUE( r.appliedLine.endsWith( QStringLiteral(" flavor=film grade=film-v1") ) );
        ASSERT_TRUE( r.flavorOnReceipt == QLatin1String( "film" ) );
        const QString scene = appliedField( r.appliedLine, QStringLiteral("scene") );
        ASSERT_TRUE( scene == appliedField( master.applied, QStringLiteral("scene") ) );
        ASSERT_TRUE( withoutFlavorField( r.appliedLine ) == master.applied );
        ASSERT_EQ( classic.value( QStringLiteral("temp") ), film.value( QStringLiteral("temp") ) );
        ASSERT_EQ( classic.value( QStringLiteral("tint") ), film.value( QStringLiteral("tint") ) );
        ASSERT_EQ( classic.value( QStringLiteral("exp") ), film.value( QStringLiteral("exp") ) );
        ASSERT_EQ( classic.value( QStringLiteral("chromaSmooth") ), film.value( QStringLiteral("chromaSmooth") ) );
        const LookAssistFlavorDeltas d = lookAssistCinematicDeltasForScene( sceneByName( scene ) );
        ASSERT_EQ( clampInt( -100, classic.value( QStringLiteral("contrast") ) + d.contrast, 100 ), film.value( QStringLiteral("contrast") ) );
        ASSERT_EQ( clampInt( 0, classic.value( QStringLiteral("pivot") ) + d.pivot, 100 ), film.value( QStringLiteral("pivot") ) );
        ASSERT_EQ( clampInt( -100, classic.value( QStringLiteral("shadows") ) + d.shadows, 100 ), film.value( QStringLiteral("shadows") ) );
        ASSERT_EQ( clampInt( -100, classic.value( QStringLiteral("highlights") ) + d.highlights, 100 ), film.value( QStringLiteral("highlights") ) );
        ASSERT_EQ( clampInt( -100, classic.value( QStringLiteral("vibrance") ) + d.vibrance, 100 ), film.value( QStringLiteral("vibrance") ) );
        ASSERT_TRUE( r.gradationCurve == lookAssistFilmGradationCurve( sceneByName( scene ) ) );
        ASSERT_TRUE( r.sha256 == master.sha256 );
    }
}

TEST(LookAssistFilmGrade, AUserCurveIsKeptAndReported)
{
    FlavorEnv env( "film" );
    MlvPipelineFixture fixture;
    ASSERT_TRUE( openFirstFixture( fixture ) );
    ReceiptSettings &receipt = fixture.receipt();
    const QString userCurve = QStringLiteral("1e-05;1e-05;0.5;0.56;1;1;?1e-05;1e-05;1;1;?1e-05;1e-05;1;1;?1e-05;1e-05;1;1;");
    ASSERT_FALSE( lookAssistIsDefaultGradationCurve( userCurve ) );
    receipt.setGradationCurve( userCurve );
    QString line;
    ASSERT_TRUE( applyCapturingLine( fixture, receipt, &line ) );
    ASSERT_TRUE( receipt.gradationCurve() == userCurve );
    ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve().isEmpty() );
    ASSERT_TRUE( receipt.lookAssistFlavor() == QLatin1String( "film" ) );
    ASSERT_TRUE( line.endsWith( QStringLiteral(" flavor=film grade=skipped_user_curve") ) );
}

TEST(LookAssistFilmGrade, TheBaselineRoundTripLeavesNoTraceOfTheGrade)
{
    // Film, then a re-run as Classic, on one receipt; against a receipt that only ever ran Classic (twice, so both went
    // through the same capture-then-restore sequence).
    QByteArray filmThenClassic, filmXml, classicOnly;
    QString filmScene;
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFirstFixture( fixture ) );
        ReceiptSettings &receipt = fixture.receipt();
        const QString before = receipt.gradationCurve();
        ASSERT_TRUE( lookAssistIsDefaultGradationCurve( before ) );
        QString line;
        {
            FlavorEnv env( "film" );
            ASSERT_TRUE( applyCapturingLine( fixture, receipt, &line ) );
        }
        filmScene = appliedField( line, QStringLiteral("scene") );
        ASSERT_TRUE( line.endsWith( QStringLiteral(" flavor=film grade=film-v1") ) );
        ASSERT_TRUE( receipt.gradationCurve() == lookAssistFilmGradationCurve( sceneByName( filmScene ) ) );
        ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve() == before );   // the default it replaced
        filmXml = lookAssistReceiptXml( receipt );
        ASSERT_TRUE( filmXml.contains( "<lookAssistBaselineGradationCurve>" ) );
        {
            FlavorEnv env( "classic" );
            ASSERT_TRUE( applyCapturingLine( fixture, receipt, &line ) );
        }
        ASSERT_TRUE( line.endsWith( QStringLiteral(" flavor=classic") ) );
        ASSERT_TRUE( receipt.gradationCurve() == before );
        ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve().isEmpty() );
        filmThenClassic = lookAssistReceiptXml( receipt );
    }
    {
        FlavorEnv env( "classic" );
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFirstFixture( fixture ) );
        ReceiptSettings &receipt = fixture.receipt();
        ASSERT_TRUE( applyCapturingLine( fixture, receipt, nullptr ) );
        ASSERT_FALSE( lookAssistReceiptXml( receipt ).contains( "lookAssistBaselineGradationCurve" ) );
        ASSERT_TRUE( applyCapturingLine( fixture, receipt, nullptr ) );
        classicOnly = lookAssistReceiptXml( receipt );
    }
    ASSERT_FALSE( classicOnly.contains( "lookAssistBaselineGradationCurve" ) );
    ASSERT_TRUE( filmThenClassic == classicOnly );
    {
        // Cinematic never records the element either.
        FlavorEnv env( "cinematic" );
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFirstFixture( fixture ) );
        ReceiptSettings &receipt = fixture.receipt();
        const QString before = receipt.gradationCurve();
        ASSERT_TRUE( applyCapturingLine( fixture, receipt, nullptr ) );
        ASSERT_TRUE( receipt.gradationCurve() == before );
        ASSERT_FALSE( lookAssistReceiptXml( receipt ).contains( "lookAssistBaselineGradationCurve" ) );
    }
    {
        // Film, then Look Assist switched off: the curve goes back, the element goes away.
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFirstFixture( fixture ) );
        ReceiptSettings &receipt = fixture.receipt();
        const QString before = receipt.gradationCurve();
        {
            FlavorEnv env( "film" );
            ASSERT_TRUE( applyCapturingLine( fixture, receipt, nullptr ) );
        }
        ASSERT_FALSE( lookAssistIsDefaultGradationCurve( receipt.gradationCurve() ) );
        receipt.setLookAssistEnabled( false );
        ASSERT_FALSE( applyCapturingLine( fixture, receipt, nullptr ) );
        ASSERT_TRUE( receipt.gradationCurve() == before );
        ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve().isEmpty() );
    }
}

TEST(LookAssistFilmGrade, AUserEditAfterTheFilmGradeIsKeptByAClassicReRunAndByLookAssistOff)
{
    // Film laid over the default curve -> the user adds a Y point (0.5,0.56) to the laid curve -> a re-run as Classic, or
    // Look Assist switched off. The edit is the user's curve: it is kept, and the baseline goes (Film's ownership is
    // retired), so a later Film run reports skipped_user_curve instead of laying over it.
    const auto userEdit = []( const QString &laid ) {
        QStringList lines = laid.split( QLatin1Char('?') );
        lines[0] = QStringLiteral("1e-05;1e-05;0.5;0.56;1;1;");
        return lines.join( QLatin1Char('?') );
    };
    for( int offAfterEdit = 0; offAfterEdit < 2; ++offAfterEdit )
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFirstFixture( fixture ) );
        ReceiptSettings &receipt = fixture.receipt();
        ASSERT_TRUE( lookAssistIsDefaultGradationCurve( receipt.gradationCurve() ) );
        QString line;
        {
            FlavorEnv env( "film" );
            ASSERT_TRUE( applyCapturingLine( fixture, receipt, &line ) );
        }
        ASSERT_TRUE( line.endsWith( QStringLiteral(" flavor=film grade=film-v1") ) );
        ASSERT_FALSE( receipt.lookAssistBaselineGradationCurve().isEmpty() );
        const QString edited = userEdit( receipt.gradationCurve() );
        ASSERT_FALSE( lookAssistIsDefaultGradationCurve( edited ) );
        receipt.setGradationCurve( edited );
        if( offAfterEdit )
        {
            receipt.setLookAssistEnabled( false );
            ASSERT_FALSE( applyCapturingLine( fixture, receipt, nullptr ) );
        }
        else
        {
            FlavorEnv env( "classic" );
            ASSERT_TRUE( applyCapturingLine( fixture, receipt, &line ) );
            ASSERT_TRUE( line.endsWith( QStringLiteral(" flavor=classic") ) );
        }
        ASSERT_TRUE( receipt.gradationCurve() == edited );
        ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve().isEmpty() );
        ASSERT_FALSE( lookAssistReceiptXml( receipt ).contains( "lookAssistBaselineGradationCurve" ) );
        if( !offAfterEdit )
        {
            FlavorEnv env( "film" );
            ASSERT_TRUE( applyCapturingLine( fixture, receipt, &line ) );
            ASSERT_TRUE( line.endsWith( QStringLiteral(" flavor=film grade=skipped_user_curve") ) );
            ASSERT_TRUE( receipt.gradationCurve() == edited );
            ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve().isEmpty() );
        }
    }
}

TEST(LookAssistFilmGrade, FilmIsADifferentGradedPictureFromCinematic)
{
    // With the sliders and the curve applied the way the app applies them, Film is a different, deterministic picture.
    const FixtureCase &c = fixtureCases().front();
    Run cine, film, filmAgain;
    {
        FlavorEnv env( "cinematic" );
        cine = run( c, true, true );
    }
    {
        FlavorEnv env( "film" );
        film = run( c, true, true );
        filmAgain = run( c, true, true );
    }
    ASSERT_TRUE( cine.ok && film.ok && filmAgain.ok );
    ASSERT_TRUE( film.receipt == cine.receipt );            // the same sliders: the difference is the grade
    ASSERT_TRUE( film.sha256 != cine.sha256 );
    ASSERT_TRUE( film.sha256 == filmAgain.sha256 );
}
