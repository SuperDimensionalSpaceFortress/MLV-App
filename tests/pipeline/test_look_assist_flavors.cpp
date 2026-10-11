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

extern "C" void fromRGBtoHSV( float rgb[], float hsv[] );   // src/processing/processing.c

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

// ---- LOOK-ASSIST-FILM-FLAVOR-1/-2/-3: the Film grade (Cinematic's tone + film-v3: a teal / warm split in R, G, B with a
// neutral blue-amber lean, and a Y tone line) ----

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

// LOOK-ASSIST-FILM-FLAVOR-2: one channel through the whole gradation stage (Y, then the channel's own table), minus the
// default curve's, at every 16-bit input. The green-magenta move of any pixel is dG(g) - (dR(r) + dB(b)) / 2.
struct StageDeltas
{
    std::vector<int> r, g, b;
};
StageDeltas stageDeltas( const GradationTables &t, const GradationTables &def )
{
    StageDeltas d;
    d.r.resize( 65536 );
    d.g.resize( 65536 );
    d.b.resize( 65536 );
    for( int v = 0; v < 65536; ++v )
    {
        d.r[v] = static_cast<int>( t.r[t.y[v]] ) - static_cast<int>( def.r[def.y[v]] );
        d.g[v] = static_cast<int>( t.g[t.y[v]] ) - static_cast<int>( def.g[def.y[v]] );
        d.b[v] = static_cast<int>( t.b[t.y[v]] ) - static_cast<int>( def.b[def.y[v]] );
    }
    return d;
}

// The split S of a neutral 8-bit ramp through the stage, as the venue tool measures it on tiles: mean(B - R) over the
// shadow band of input codes [8, 40] minus mean(B - R) over the highlight band [85, 140] (the #319 r1c trio's bands), in
// 8-bit code values.
double neutralSplit8( const GradationTables &t )
{
    const auto bandMean = [&t]( int lo, int hi ) {
        double sum = 0.0;
        for( int c = lo; c <= hi; ++c )
        {
            const int v = tableIndex( c / 255.0 );
            sum += ( static_cast<double>( t.b[t.y[v]] ) - static_cast<double>( t.r[t.y[v]] ) ) / 257.0;
        }
        return sum / ( hi - lo + 1 );
    };
    return bandMean( 8, 40 ) - bandMean( 85, 140 );
}

// HSV hue of one display-referred pixel after the stage, exactly as the engine's fromRGBtoHSV computes it.
double gradedHue( const GradationTables &t, double r, double g, double b )
{
    float rgb[3] = { t.r[t.y[tableIndex( r )]] / 65535.0f, t.g[t.y[tableIndex( g )]] / 65535.0f,
                     t.b[t.y[tableIndex( b )]] / 65535.0f };
    float hsv[3] = { 0.0f, 0.0f, 0.0f };
    fromRGBtoHSV( rgb, hsv );
    return hsv[0];
}

// LOOK-ASSIST-FILM-FLAVOR-3: the blue-amber lean of the neutral ramp through the stage, as look-flavor-diff.py warmcool
// defines it (rampLean): mean over every 16-bit code v of ((R + G) / 2 - B) / 65535, each channel through Y then its own
// table. Positive = amber; the default curve's is 0.
double rampLean( const GradationTables &t )
{
    double sum = 0.0;
    for( int v = 0; v < 65536; ++v )
    {
        const int y = t.y[v];
        sum += ( ( static_cast<double>( t.r[y] ) + static_cast<double>( t.g[y] ) ) / 2.0 - static_cast<double>( t.b[y] ) ) / 65535.0;
    }
    return sum / 65536.0;
}

// The warm-cool bound every lean is held to: the app's own neutrality bar (the white-balance search calls a surface
// neutral at |B - R| < 2 codes, kRefineDeadBand; a pure R - B offset of 2 is a lean of 1.5 codes).
const double kWarmCoolNeutralBound = 1.5 / 255.0;

// The pre-registered film-v3 candidate grid (t, w per unit strength; LOOK-ASSIST-FILM-FLAVOR-3), fixed before any table was
// built. The shipped constants are the first candidate, in this order, that passed every table gate.
struct FilmCandidate { const char *id; double teal; double warm; };
const FilmCandidate kFilmV3Grid[] = { { "C1", 0.042, 0.024 }, { "C2", 0.042, 0.020 }, { "C3", 0.038, 0.024 }, { "C4", 0.046, 0.027 } };

LookAssistFilmGrade filmCandidateGrade( LookAssistScene scene, const FilmCandidate &c )
{
    LookAssistFilmGrade g = lookAssistFilmGradeForScene( scene );
    g.tealOffset = c.teal * g.strength;
    g.warmOffset = c.warm * g.strength;
    return g;
}

// The green-magenta axis of a grade against the default curve's stage, in 8-bit codes: the neutral range (R = G = B) and
// the per-channel any-pixel bound [min dG - (max dR + max dB)/2, max dG - (min dR + min dB)/2].
struct GreenAxis
{
    double neutralLo = 1e9, neutralHi = -1e9, lo = 0.0, hi = 0.0;
};
GreenAxis greenAxis( const GradationTables &film, const GradationTables &def )
{
    GreenAxis a;
    const StageDeltas d = stageDeltas( film, def );
    for( int v = 0; v < 65536; ++v )
    {
        const double dGA = ( d.g[v] - ( d.r[v] + d.b[v] ) / 2.0 ) / 257.0;
        a.neutralLo = std::min( a.neutralLo, dGA );
        a.neutralHi = std::max( a.neutralHi, dGA );
    }
    const auto mm = []( const std::vector<int> &v ) { return std::minmax_element( v.begin(), v.end() ); };
    const auto r = mm( d.r ), g = mm( d.g ), b = mm( d.b );
    a.lo = ( *g.first - ( *r.second + *b.second ) / 2.0 ) / 257.0;
    a.hi = ( *g.second - ( *r.first + *b.first ) / 2.0 ) / 257.0;
    return a;
}

// #319's coloured set and the direction each moves on the green-magenta axis (+1 toward green).
struct Coloured { const char *name; double r, g, b; int sign; };
const Coloured kColouredSet[] = {
    { "amber", 0.72, 0.45, 0.18, -1 },
    { "teal", 0.18, 0.45, 0.72, +1 },
    { "skin", 0.80, 0.55, 0.35, +1 },
    { "sky", 0.30, 0.55, 0.85, +1 },
    { "sodium", 0.90, 0.50, 0.10, -1 },
    { "cyan", 0.10, 0.50, 0.90, +1 },
};

// The skin patches R:G:B = 1:0.72:0.56 and 1:0.80:0.68 at several levels (the #319 guard's set).
struct Patch { double g, b, r; };
const Patch kSkinPatches[] = { { 0.72, 0.56, 0.35 }, { 0.72, 0.56, 0.50 }, { 0.72, 0.56, 0.65 }, { 0.72, 0.56, 0.80 },
                               { 0.80, 0.68, 0.55 }, { 0.80, 0.68, 0.75 }, { 0.80, 0.68, 0.90 } };

// Over the skin patches through one grade's stage, against the default's: the graded hue range, the most negative dh and
// the largest magenta-ward move (max of h_default - h_graded, 0 when none moves that way).
struct SkinHue
{
    double hueLo = 1e9, hueHi = -1e9, dhLo = 1e9, magentaWard = 0.0;
};
SkinHue skinHue( const GradationTables &film, const GradationTables &def )
{
    SkinHue s;
    for( const Patch &p : kSkinPatches )
    {
        const double r = p.r, g = p.g * p.r, b = p.b * p.r;
        const double h0 = gradedHue( def, r, g, b ), h = gradedHue( film, r, g, b );
        s.hueLo = std::min( s.hueLo, h );
        s.hueHi = std::max( s.hueHi, h );
        s.dhLo = std::min( s.dhLo, h - h0 );
        s.magentaWard = std::max( s.magentaWard, h0 - h );
    }
    return s;
}

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

TEST(LookAssistFilmGrade, FilmV3CurveIsTealGoldWithANeutralLean)
{
    // film-v3's four tables, against the default receipt's. R and B move by equal and opposite amounts at every knot and
    // the spline is linear in y, so r + b == 2 def on every entry the stage can reach (only rounding is left); the
    // engine's 0.0001 floor clamps R's dip below the teal knot on a few entries under Y[0], which the Y line never
    // indexes. G takes the share k of each offset; between knots the natural spline dips it below the default by a
    // fraction of one 8-bit code (measured, printed). Y is the lift / shoulder line.
    const GradationTables def = gradationTables( defaultCurve() );
    ASSERT_EQ( 65536, static_cast<int>( def.g.size() ) );
    const int floor16 = static_cast<int>( 0.0001f * 65535.0 );   // processingSetGCurve's clamp, as a table value
    for( LookAssistScene scene : kFilmScenes )
    {
        const LookAssistFilmGrade grade = lookAssistFilmGradeForScene( scene );
        const GradationTables film = gradationTables( lookAssistFilmGradationCurve( scene ) );
        int worstMirror = 0, clampedAbove = -1, greenDip = 0, greenDipAt = 0;
        for( int v = 0; v < 65536; ++v )
        {
            if( film.r[v] <= floor16 || film.b[v] <= floor16 )
            {
                clampedAbove = v;
                continue;
            }
            const int mirror = std::abs( static_cast<int>( film.r[v] ) + static_cast<int>( film.b[v] ) - 2 * static_cast<int>( def.g[v] ) );
            worstMirror = std::max( worstMirror, mirror );
            const int dg = static_cast<int>( film.g[v] ) - static_cast<int>( def.g[v] );
            if( dg < greenDip ) { greenDip = dg; greenDipAt = v; }
        }
        std::fprintf( stderr, "FILM-V3-CURVE scene=%s max|r+b-2def|=%d floor_clamped_entries<=%d Y[0]=%d Y[32768]=%d Y[65535]=%d"
                      " min(G-def)=%d at x=%.4f\n", qPrintable( lookAssistSceneName( scene ) ), worstMirror, clampedAbove,
                      film.y[0], film.y[32768], film.y[65535], greenDip, greenDipAt / 65535.0 );
        ASSERT_TRUE( worstMirror <= 2 );
        ASSERT_TRUE( clampedAbove < static_cast<int>( film.y[0] ) );   // the clamped entries are unreachable through Y
        ASSERT_TRUE( greenDip >= -257 );                                // never a whole 8-bit code below the default
        // Y: lifted at 0, through the mid-point, rolled at 1 -- the documented line, not the default.
        ASSERT_TRUE( film.y != def.y );
        ASSERT_TRUE( std::abs( static_cast<int>( film.y[0] ) - static_cast<int>( grade.blackLift * 65535.0 ) ) <= 2 );
        ASSERT_TRUE( std::abs( static_cast<int>( film.y[32768] ) - 32767 ) <= 2 );
        ASSERT_TRUE( std::abs( static_cast<int>( film.y[65535] ) - static_cast<int>( ( 1.0 - grade.whiteRoll ) * 65535.0 ) ) <= 3 );
    }
}

TEST(LookAssistFilmGrade, FilmV3RampLeanIsNeutral)
{
    // The owner's question (2026-10-09): Film is warm. v2's warm half covered three quarters of the tonal range, so an
    // evenly exposed picture leaned amber by construction. v3's blue-amber lean on the neutral ramp, through the engine's
    // stage over all 65536 codes, is within the app's own neutrality bar (1.5 / 255) at every scene, both ways. v2 must
    // read above the bar at Shade: the test can see the lean it exists to remove.
    for( LookAssistScene scene : kFilmScenes )
    {
        const double v3 = rampLean( gradationTables( lookAssistFilmGradationCurve( scene ) ) );
        const double v2 = rampLean( gradationTables( lookAssistFilmGradationCurveV2( scene ) ) );
        const double v1 = rampLean( gradationTables( lookAssistFilmGradationCurveV1( scene ) ) );
        std::fprintf( stderr, "FILM-V3-RAMP-LEAN scene=%s v3=%+.6f (%+.3f/255) v2=%+.6f (%+.3f/255) v1=%+.6f (%+.3f/255) bound=%.6f\n",
                      qPrintable( lookAssistSceneName( scene ) ), v3, v3 * 255.0, v2, v2 * 255.0, v1, v1 * 255.0, kWarmCoolNeutralBound );
        ASSERT_TRUE( std::fabs( v3 ) <= kWarmCoolNeutralBound );
        if( scene == LookAssistScene::Shade ) ASSERT_TRUE( v2 > kWarmCoolNeutralBound );
    }
    ASSERT_TRUE( std::fabs( rampLean( gradationTables( defaultCurve() ) ) ) < 1e-12 );
}

TEST(LookAssistFilmGrade, FilmV3IsNotV2)
{
    // At every scene v3, the legacy v2 and the legacy v1 are three different curves and three different table sets, and
    // none of them is the default. v3 is not v2's recipe either: its warm knot, its neutral zone's end and its warm offset
    // are not v2's (the R line's knots, as the widget parses them).
    const GradationTables def = gradationTables( defaultCurve() );
    const auto same = []( const GradationTables &a, const GradationTables &b ) { return a.y == b.y && a.r == b.r && a.g == b.g && a.b == b.b; };
    for( LookAssistScene scene : kFilmScenes )
    {
        const QString c3 = lookAssistFilmGradationCurve( scene ), c2 = lookAssistFilmGradationCurveV2( scene ),
                      c1 = lookAssistFilmGradationCurveV1( scene );
        ASSERT_TRUE( c3 != c2 && c2 != c1 && c3 != c1 );
        std::vector<LookAssistGradationPoint> l3[4], l2[4];
        ASSERT_EQ( 4, lookAssistParseGradationCurve( c3, l3 ) );
        ASSERT_EQ( 4, lookAssistParseGradationCurve( c2, l2 ) );
        ASSERT_EQ( 7, static_cast<int>( l3[1].size() ) );
        ASSERT_EQ( 7, static_cast<int>( l2[1].size() ) );
        ASSERT_TRUE( std::fabs( l3[1][3].x - l2[1][3].x ) > 1e-3 );                                  // neutral end 0.28 vs 0.26
        ASSERT_TRUE( std::fabs( l3[1][4].x - l2[1][4].x ) > 1e-3 );                                  // warm knot 0.48 vs 0.52
        ASSERT_TRUE( std::fabs( ( l3[1][4].y - l3[1][4].x ) - ( l2[1][4].y - l2[1][4].x ) ) > 1e-3 );  // w: v3's vs v2's
        const GradationTables t3 = gradationTables( c3 ), t2 = gradationTables( c2 ), t1 = gradationTables( c1 );
        ASSERT_FALSE( same( t3, t2 ) );
        ASSERT_FALSE( same( t2, t1 ) );
        ASSERT_FALSE( same( t3, t1 ) );
        for( const QString &c : { c3, c2, c1 } ) ASSERT_FALSE( lookAssistIsDefaultGradationCurve( c ) );
        for( const GradationTables *t : { &t3, &t2, &t1 } ) ASSERT_FALSE( same( *t, def ) );
    }
}

TEST(LookAssistFilmGrade, FilmV3NeutralLeansGreenNeverMagenta)
{
    // Per pixel, through the engine's own stage (Y, then each channel's table) at each Film strength, against the default
    // curve's stage. Any pixel's green-magenta move is dG(g) - (dR(r) + dB(b)) / 2 with each term a function of one
    // channel only, so the tables bound it for EVERY pixel: [min dG - (max dR + max dB)/2, max dG - (min dR + min dB)/2].
    // (a) Neutral (R = G = B): the move is G's share, +k * offset, so it leans toward green and never toward magenta:
    //     every grey level in [-0.5, +2.5] 8-bit codes, and the lean is really there (>= 60% of k * w at the warm knot).
    // (b) Any pixel: within the per-channel bound, pinned below at the documented values (docs/look-assist-flavors.md),
    //     printed with the share the Y line alone takes (the Y line is a tone curve: it moves chroma on every route).
    // (c) #319's coloured set: within the bound and in the documented direction.
    const GradationTables def = gradationTables( defaultCurve() );
    // The documented any-pixel bounds (8-bit codes, rounded outward to 0.1 from the film-v3 values this test printed),
    // Night / ArtificialLights / Shade / BrightSun.
    const double documentedLower[] = { -9.0, -13.4, -17.9, -16.1 };
    const double documentedUpper[] = { 6.7, 10.1, 13.5, 12.1 };
    for( LookAssistScene scene : kFilmScenes )
    {
        const int s = static_cast<int>( scene );
        const LookAssistFilmGrade grade = lookAssistFilmGradeForScene( scene );
        const GradationTables film = gradationTables( lookAssistFilmGradationCurve( scene ) );
        const GreenAxis a = greenAxis( film, def );
        const double lean = 0.6 * 0.15 * grade.warmOffset * 255.0;   // the documented k, not the code's (a k of 0 must fail here)
        GradationTables toneOnly = def;
        toneOnly.y = film.y;
        const GreenAxis tone = greenAxis( toneOnly, def );

        std::fprintf( stderr, "FILM-V3-GREEN-AXIS scene=%s s=%.2f neutral dGA=[%+.4f,%+.4f] lean_floor=%.4f any_pixel=[%+.4f,%+.4f]"
                      " y_line_alone=[%+.4f,%+.4f] documented=[%+.1f,%+.1f]\n", qPrintable( lookAssistSceneName( scene ) ),
                      grade.strength, a.neutralLo, a.neutralHi, lean, a.lo, a.hi, tone.lo, tone.hi, documentedLower[s], documentedUpper[s] );
        for( const Coloured &c : kColouredSet )
        {
            const double dGA = gradedGreenAxis8( film, c.r, c.g, c.b ) - gradedGreenAxis8( def, c.r, c.g, c.b );
            std::fprintf( stderr, "FILM-V3-GREEN-AXIS scene=%s input=%s rgb=%.2f/%.2f/%.2f dGA=%+.4f\n",
                          qPrintable( lookAssistSceneName( scene ) ), c.name, c.r, c.g, c.b, dGA );
        }
        ASSERT_TRUE( a.neutralLo >= -0.5 );
        ASSERT_TRUE( a.neutralHi <= 2.5 );
        ASSERT_TRUE( a.neutralHi >= lean );
        ASSERT_TRUE( a.lo >= documentedLower[s] );
        ASSERT_TRUE( a.hi <= documentedUpper[s] );
        for( const Coloured &c : kColouredSet )
        {
            const double dGA = gradedGreenAxis8( film, c.r, c.g, c.b ) - gradedGreenAxis8( def, c.r, c.g, c.b );
            ASSERT_TRUE( dGA >= a.lo - 1e-9 && dGA <= a.hi + 1e-9 );
            ASSERT_TRUE( dGA * c.sign > 0.0 );
        }
    }
}

TEST(LookAssistFilmGrade, FilmCurveIsPinned)
{
    // film-v3: the four 65536-entry tables per scene by sha256, and sampled values, as the engine built them at this commit.
    struct Pin { LookAssistScene scene; const char *sha256; int y[5]; int r[5]; int g[5]; int b[5]; };
    const Pin pins[] = {
        { LookAssistScene::Night, "cfdb9f5ca22e2b83b15c30d13d7a75e9811dcceca6f60f6fbefa6198ccc7ae5b",
          { 654, 7049, 16014, 31471, 55243 }, { 6, 5308, 15824, 32242, 55553 }, { 6, 6740, 15707, 31574, 55685 }, { 6, 7799, 15631, 30670, 55855 } },
        { LookAssistScene::ArtificialLights, "b4998eb039808cdb3e41649e28b798eec1d96313bf846034e78c15b85aff2b0e",
          { 982, 7298, 16158, 31479, 55013 }, { 6, 4686, 15872, 32636, 55477 }, { 6, 6834, 15696, 31633, 55675 }, { 6, 8421, 15583, 30276, 55930 } },
        { LookAssistScene::Shade, "913cbec93de9e935263e009d7361182bab2e4636858c1171ea2a4a72d2977a91",
          { 1310, 7546, 16302, 31487, 54783 }, { 6, 4063, 15920, 33029, 55402 }, { 6, 6927, 15686, 31692, 55666 }, { 6, 9044, 15534, 29883, 56006 } },
        { LookAssistScene::BrightSun, "904d7d4bc041fdaa49daa124aa462926a5f92ab8e9c26a1af70e0005f838eb5e",
          { 1179, 7447, 16244, 31484, 54875 }, { 6, 4312, 15901, 32872, 55432 }, { 6, 6890, 15690, 31668, 55670 }, { 6, 8795, 15554, 30040, 55975 } },
    };
    const double xs[5] = { 0.0, 0.10, 0.24, 0.48, 0.85 };
    for( const Pin &pin : pins )   // every scene's line first, so a re-pin reads all four from one run
    {
        const GradationTables film = gradationTables( lookAssistFilmGradationCurve( pin.scene ) );
        QString samples;
        for( int i = 0; i < 5; ++i )
        {
            const int v = tableIndex( xs[i] );
            samples += QStringLiteral(" x=%1 y=%2 r=%3 g=%4 b=%5").arg( xs[i] ).arg( film.y[v] ).arg( film.r[v] ).arg( film.g[v] ).arg( film.b[v] );
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
            ASSERT_EQ( pin.y[i], static_cast<int>( film.y[v] ) );
            ASSERT_EQ( pin.r[i], static_cast<int>( film.r[v] ) );
            ASSERT_EQ( pin.g[i], static_cast<int>( film.g[v] ) );
            ASSERT_EQ( pin.b[i], static_cast<int>( film.b[v] ) );
        }
    }
}

TEST(LookAssistFilmGrade, FilmV2CurveIsPinned)
{
    // The legacy film-v2 builder (#328), byte for byte: the pins FilmCurveIsPinned held at b2cbe893, unchanged.
    struct Pin { LookAssistScene scene; const char *sha256; int y[5]; int r[5]; int g[5]; int b[5]; };
    const Pin pins[] = {
        { LookAssistScene::Night, "e04eda3fcf58a45d8600d4810ae3d62232591d3999c0e35f2a760097b5eb3d92",
          { 654, 7049, 15373, 34063, 55243 }, { 6, 5407, 15129, 35551, 56146 }, { 6, 6725, 15059, 34298, 55770 }, { 6, 7700, 15015, 32602, 55261 } },
        { LookAssistScene::ArtificialLights, "248f691bb96e48ac4a572b9a61a76af30c9218ca6fa3765e8e19427e387efb79",
          { 982, 7298, 15524, 34056, 55013 }, { 6, 4833, 15158, 36289, 56367 }, { 6, 6811, 15053, 34409, 55803 }, { 6, 8274, 14987, 31865, 55040 } },
        { LookAssistScene::Shade, "d0ab4bac7494ac3d66b3a151e295051585b0855e561ab78a8647abf151021ba0",
          { 1310, 7546, 15675, 34049, 54783 }, { 6, 4260, 15187, 37026, 56588 }, { 6, 6897, 15046, 34519, 55836 }, { 6, 8847, 14958, 31128, 54819 } },
        { LookAssistScene::BrightSun, "21b04857fe8d03508850f9c689268dbfd4f50ebc729dcd38400539691aa6ec29",
          { 1179, 7447, 15615, 34052, 54875 }, { 6, 4489, 15175, 36731, 56500 }, { 6, 6863, 15049, 34475, 55823 }, { 6, 8618, 14969, 31423, 54907 } },
    };
    const double xs[5] = { 0.0, 0.10, 0.23, 0.52, 0.85 };
    for( const Pin &pin : pins )
    {
        const GradationTables film = gradationTables( lookAssistFilmGradationCurveV2( pin.scene ) );
        ASSERT_TRUE( tablesSha256( film ) == QLatin1String( pin.sha256 ) );
        for( int i = 0; i < 5; ++i )
        {
            const int v = tableIndex( xs[i] );
            ASSERT_EQ( pin.y[i], static_cast<int>( film.y[v] ) );
            ASSERT_EQ( pin.r[i], static_cast<int>( film.r[v] ) );
            ASSERT_EQ( pin.g[i], static_cast<int>( film.g[v] ) );
            ASSERT_EQ( pin.b[i], static_cast<int>( film.b[v] ) );
        }
    }
}

TEST(LookAssistFilmGrade, FilmV1CurveIsPinned)
{
    // The legacy film-v1 builder (#319), byte for byte: the pins FilmCurveIsPinned held at 758e978e, unchanged.
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
    for( const Pin &pin : pins )
    {
        const GradationTables film = gradationTables( lookAssistFilmGradationCurveV1( pin.scene ) );
        ASSERT_TRUE( tablesSha256( film ) == QLatin1String( pin.sha256 ) );
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
    // The inert kill, at film-v3's knots: at Shade every channel leaves the default by at least 60% of its knot offset,
    // the right way round (teal at 0.10: R down, B and G up; warm at 0.48: R and G up, B down).
    const GradationTables def = gradationTables( defaultCurve() );
    const LookAssistFilmGrade g = lookAssistFilmGradeForScene( LookAssistScene::Shade );
    const GradationTables film = gradationTables( lookAssistFilmGradationCurve( LookAssistScene::Shade ) );
    const auto off = [&def]( const std::vector<uint16_t> &t, int v ) { return static_cast<int>( t[v] ) - static_cast<int>( def.g[v] ); };
    const int lo = tableIndex( 0.10 );
    const int hi = tableIndex( 0.48 );
    const double t = g.tealOffset * 65535.0, w = g.warmOffset * 65535.0, k = g.greenShare;
    ASSERT_TRUE( off( film.r, lo ) <= -0.6 * t );
    ASSERT_TRUE( off( film.b, lo ) >= 0.6 * t );
    ASSERT_TRUE( off( film.g, lo ) >= 0.6 * k * t );
    ASSERT_TRUE( off( film.r, hi ) >= 0.6 * w );
    ASSERT_TRUE( off( film.b, hi ) <= -0.6 * w );
    ASSERT_TRUE( off( film.g, hi ) >= 0.6 * k * w );
    ASSERT_TRUE( lookAssistFilmGradationCurve( LookAssistScene::Shade ) != defaultCurve() );
    ASSERT_FALSE( lookAssistIsDefaultGradationCurve( lookAssistFilmGradationCurve( LookAssistScene::Shade ) ) );
}

TEST(LookAssistFilmGrade, FilmV3IsMateriallyStrongerThanV1)
{
    // The owner's complaint about v1 was that it is too subtle; v3 takes v2's amber lean out without going back there.
    // Through the engine's stage, on a neutral 8-bit ramp, the split S (mean B-R over input codes [8,40] minus over
    // [85,140], the #319 trio's tile bands, less the default curve's) at Shade must be at least 3x v1's AND at least 0.70x
    // v2's. v3 is never v1, and never the default, at any scene.
    const GradationTables def = gradationTables( defaultCurve() );
    const double sDefault = neutralSplit8( def );
    double v1Shade = 0.0, v2Shade = 0.0, v3Shade = 0.0;
    for( LookAssistScene scene : kFilmScenes )
    {
        const GradationTables v3 = gradationTables( lookAssistFilmGradationCurve( scene ) );
        const GradationTables v2 = gradationTables( lookAssistFilmGradationCurveV2( scene ) );
        const GradationTables v1 = gradationTables( lookAssistFilmGradationCurveV1( scene ) );
        const double s3 = neutralSplit8( v3 ) - sDefault;
        const double s2 = neutralSplit8( v2 ) - sDefault;
        const double s1 = neutralSplit8( v1 ) - sDefault;
        std::fprintf( stderr, "FILM-V3-AMPLITUDE scene=%s S_table(v1)=%.3f S_table(v2)=%.3f S_table(v3)=%.3f v3/v1=%.3f v3/v2=%.3f\n",
                      qPrintable( lookAssistSceneName( scene ) ), s1, s2, s3, s1 != 0.0 ? s3 / s1 : 0.0, s2 != 0.0 ? s3 / s2 : 0.0 );
        if( scene == LookAssistScene::Shade ) { v1Shade = s1; v2Shade = s2; v3Shade = s3; }
        ASSERT_TRUE( lookAssistFilmGradationCurve( scene ) != lookAssistFilmGradationCurveV1( scene ) );
        ASSERT_FALSE( lookAssistIsDefaultGradationCurve( lookAssistFilmGradationCurve( scene ) ) );
        ASSERT_FALSE( v3.y == def.y && v3.r == def.r && v3.g == def.g && v3.b == def.b );
        ASSERT_TRUE( s3 > s1 );
    }
    ASSERT_TRUE( v1Shade > 0.0 );
    ASSERT_TRUE( v3Shade >= 3.0 * v1Shade );
    ASSERT_TRUE( v3Shade >= 0.70 * v2Shade );
}

TEST(LookAssistFilmGrade, FilmV3SkinPatchesKeepTheirHue)
{
    // The skin-hue guard, by construction (the teal lift ends at 0.20; 0.20..0.28 is neutral; G takes a share of the warm
    // push). HSV hue as the engine's fromRGBtoHSV computes it, through the stage, against the default curve's, on skin
    // patches R:G:B = 1:0.72:0.56 and 1:0.80:0.68 at several levels, every scene:
    //   (G1) the graded hue stays in [12, 32] degrees;
    //   (G2) no patch moves more than 3.5 degrees toward magenta (dh < 0);
    //   (G3) at Shade, v3's largest magenta-ward move is no larger than v2's over the same patches.
    const GradationTables def = gradationTables( defaultCurve() );
    // Every value first (printed), then the guard's kill (G3), then G1 and G2, so a guard regression reads as G3.
    double v3MagentaShade = 0.0, v2MagentaShade = 0.0;
    std::vector<std::pair<double, double>> graded;   // (hue_v3, dh_v3) for G1 / G2
    for( LookAssistScene scene : kFilmScenes )
    {
        const GradationTables v3 = gradationTables( lookAssistFilmGradationCurve( scene ) );
        const GradationTables v2 = gradationTables( lookAssistFilmGradationCurveV2( scene ) );
        for( const Patch &p : kSkinPatches )
        {
            const double r = p.r, g = p.g * p.r, b = p.b * p.r;
            const double h0 = gradedHue( def, r, g, b );
            const double h3 = gradedHue( v3, r, g, b );
            const double h2 = gradedHue( v2, r, g, b );
            std::fprintf( stderr, "FILM-V3-SKIN scene=%s patch=1:%.2f:%.2f R=%.2f hue_default=%.3f hue_v3=%.3f dh_v3=%+.3f"
                          " hue_v2=%.3f dh_v2=%+.3f\n", qPrintable( lookAssistSceneName( scene ) ), p.g, p.b, p.r, h0, h3,
                          h3 - h0, h2, h2 - h0 );
            graded.push_back( std::make_pair( h3, h3 - h0 ) );
        }
        if( scene == LookAssistScene::Shade )
        {
            v3MagentaShade = skinHue( v3, def ).magentaWard;
            v2MagentaShade = skinHue( v2, def ).magentaWard;
        }
    }
    std::fprintf( stderr, "FILM-V3-SKIN Shade max magenta-ward v3=%.3f v2=%.3f\n", v3MagentaShade, v2MagentaShade );
    ASSERT_TRUE( v3MagentaShade <= v2MagentaShade );                // G3
    for( const std::pair<double, double> &h : graded )
    {
        ASSERT_TRUE( h.first >= 12.0 && h.first <= 32.0 );          // G1
        ASSERT_TRUE( h.second >= -3.5 );                            // G2
    }
}

TEST(LookAssistFilmGrade, FilmV3LiftsBlacksAndRollsWhites)
{
    // The gentle black lift and the soft shoulder, at every scene: Y[0] >= 0.015 s and Y[65535] <= 1 - 0.02 s (of 65535),
    // and every one of the four tables is monotonic non-decreasing (no tone reversal anywhere). v3's Y line is v2's.
    for( LookAssistScene scene : kFilmScenes )
    {
        const LookAssistFilmGrade g = lookAssistFilmGradeForScene( scene );
        const GradationTables film = gradationTables( lookAssistFilmGradationCurve( scene ) );
        bool monotonic = true;
        for( const std::vector<uint16_t> *table : { &film.y, &film.r, &film.g, &film.b } )
            for( int v = 1; v < 65536; ++v )
                if( ( *table )[v] < ( *table )[v - 1] ) monotonic = false;
        std::fprintf( stderr, "FILM-V3-TONE scene=%s Y[0]=%d (floor %.1f) Y[65535]=%d (ceiling %.1f) monotonic=%d\n",
                      qPrintable( lookAssistSceneName( scene ) ), film.y[0], 0.015 * g.strength * 65535.0, film.y[65535],
                      ( 1.0 - 0.02 * g.strength ) * 65535.0, monotonic ? 1 : 0 );
        ASSERT_TRUE( film.y[0] >= 0.015 * g.strength * 65535.0 );
        ASSERT_TRUE( film.y[65535] <= ( 1.0 - 0.02 * g.strength ) * 65535.0 );
        ASSERT_TRUE( monotonic );
        ASSERT_TRUE( film.y == gradationTables( lookAssistFilmGradationCurveV2( scene ) ).y );
    }
}

TEST(LookAssistFilmGrade, DumpTablesWhenAsked)
{
    // Frame-locked comparison tooling (tools/profiling/look-flavor-diff.py regrade / warmcool): with
    // MLVAPP_FILM_TABLE_DUMP_DIR set, writes the engine-built tables of film-v1, film-v2 and film-v3 for every scene
    // (film-<v1|v2|v3>-<scene>.u16), and of the four pre-registered v3 candidates (film-v3-<C1..C4>-<scene>.u16), each
    // file 4 x 65536 uint16 little-endian in Y, R, G, B order. For each candidate it also prints the C++ table gates
    // (FILM-V3-GRID lines: ramp lean, S_table against v1 / v2, the neutral green axis and the coloured set, skin G1..G3).
    // Unset: a pass with no output. No binary is tracked.
    const QString dirPath = QString::fromLocal8Bit( qgetenv( "MLVAPP_FILM_TABLE_DUMP_DIR" ) );
    if( dirPath.isEmpty() ) return;
    QDir dir( dirPath );
    ASSERT_TRUE( dir.mkpath( QStringLiteral(".") ) );
    const auto writeTable = [&dir]( const GradationTables &t, const QString &name ) {
        QByteArray bytes;
        for( const std::vector<uint16_t> *table : { &t.y, &t.r, &t.g, &t.b } )
            for( uint16_t value : *table )
            {
                bytes.append( static_cast<char>( value & 0xff ) );
                bytes.append( static_cast<char>( value >> 8 ) );
            }
        if( static_cast<int>( bytes.size() ) != 4 * 65536 * 2 ) return false;
        QFile out( dir.filePath( name ) );
        if( !out.open( QIODevice::WriteOnly ) || out.write( bytes ) != static_cast<qint64>( bytes.size() ) ) return false;
        std::fprintf( stderr, "FILM-TABLE-DUMP %s sha256=%s\n", qPrintable( out.fileName() ), qPrintable( tablesSha256( t ) ) );
        return true;
    };
    for( LookAssistScene scene : kFilmScenes )
        for( int version = 1; version <= 3; ++version )
        {
            const GradationTables t = gradationTables( version == 3 ? lookAssistFilmGradationCurve( scene )
                                                       : version == 2 ? lookAssistFilmGradationCurveV2( scene )
                                                                      : lookAssistFilmGradationCurveV1( scene ) );
            ASSERT_TRUE( writeTable( t, QStringLiteral("film-v%1-%2.u16").arg( version ).arg( lookAssistSceneName( scene ) ) ) );
        }

    const GradationTables def = gradationTables( defaultCurve() );
    const double sDefault = neutralSplit8( def );
    const double sV1Shade = neutralSplit8( gradationTables( lookAssistFilmGradationCurveV1( LookAssistScene::Shade ) ) ) - sDefault;
    const GradationTables v2Shade = gradationTables( lookAssistFilmGradationCurveV2( LookAssistScene::Shade ) );
    const double sV2Shade = neutralSplit8( v2Shade ) - sDefault;
    const double v2MagentaShade = skinHue( v2Shade, def ).magentaWard;
    for( const FilmCandidate &c : kFilmV3Grid )
    {
        bool ramp = true, neutral = true, coloured = true, g1 = true, g2 = true;
        double sShade = 0.0, magentaShade = 0.0;
        for( LookAssistScene scene : kFilmScenes )
        {
            const GradationTables t = gradationTables( lookAssistFilmGradationCurve( filmCandidateGrade( scene, c ) ) );
            ASSERT_TRUE( writeTable( t, QStringLiteral("film-v3-%1-%2.u16").arg( QLatin1String( c.id ) ).arg( lookAssistSceneName( scene ) ) ) );
            const double lean = rampLean( t );
            const GreenAxis a = greenAxis( t, def );
            QString signs;
            for( const Coloured &col : kColouredSet )
            {
                const double dGA = gradedGreenAxis8( t, col.r, col.g, col.b ) - gradedGreenAxis8( def, col.r, col.g, col.b );
                signs += QStringLiteral(" %1=%2").arg( QLatin1String( col.name ) ).arg( dGA, 0, 'f', 4 );
                if( !( dGA * col.sign > 0.0 ) ) coloured = false;
            }
            const SkinHue skin = skinHue( t, def );
            const double s = neutralSplit8( t ) - sDefault;
            if( scene == LookAssistScene::Shade ) { sShade = s; magentaShade = skin.magentaWard; }
            ramp = ramp && std::fabs( lean ) <= kWarmCoolNeutralBound;
            neutral = neutral && a.neutralLo >= -0.5 && a.neutralHi <= 2.5;
            g1 = g1 && skin.hueLo >= 12.0 && skin.hueHi <= 32.0;
            g2 = g2 && skin.dhLo >= -3.5;
            std::fprintf( stderr, "FILM-V3-GRID %s t=%.3f w=%.3f scene=%s rampLean=%+.6f (%+.3f/255) S_table=%.3f neutral_dGA=[%+.4f,%+.4f]"
                          " any_pixel=[%+.4f,%+.4f] skin_hue=[%.3f,%.3f] skin_dh_min=%+.3f skin_magenta_ward=%.3f coloured:%s\n",
                          c.id, c.teal, c.warm, qPrintable( lookAssistSceneName( scene ) ), lean, lean * 255.0, s,
                          a.neutralLo, a.neutralHi, a.lo, a.hi, skin.hueLo, skin.hueHi, skin.dhLo, skin.magentaWard, qPrintable( signs ) );
        }
        const bool strongerV1 = sShade >= 3.0 * sV1Shade, strongerV2 = sShade >= 0.70 * sV2Shade, g3 = magentaShade <= v2MagentaShade;
        std::fprintf( stderr, "FILM-V3-GRID-GATES %s WC-RAMP=%s FILM-STRONGER(C++ S_table %.3f >= 3.0 x v1 %.3f: %s; >= 0.70 x v2 %.3f: %s)"
                      " NO-MAGENTA(C++ neutral in [-0.5,+2.5]: %s; coloured signs: %s) SKIN(G1: %s; G2: %s; G3 %.3f <= v2 %.3f: %s)\n",
                      c.id, ramp ? "PASS" : "FAIL", sShade, sV1Shade, strongerV1 ? "PASS" : "FAIL", sV2Shade, strongerV2 ? "PASS" : "FAIL",
                      neutral ? "PASS" : "FAIL", coloured ? "PASS" : "FAIL", g1 ? "PASS" : "FAIL", g2 ? "PASS" : "FAIL",
                      magentaShade, v2MagentaShade, g3 ? "PASS" : "FAIL" );
    }
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
        ASSERT_TRUE( r.appliedLine.endsWith( QStringLiteral(" flavor=film grade=film-v3") ) );
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
        ASSERT_TRUE( line.endsWith( QStringLiteral(" flavor=film grade=film-v3") ) );
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
        ASSERT_TRUE( line.endsWith( QStringLiteral(" flavor=film grade=film-v3") ) );
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

TEST(LookAssistFilmGrade, AV1ReceiptReRunAsFilmLaysV3)
{
    // A receipt saved under #319: the film-v1 Shade curve laid over the default, the default on record as the baseline.
    // Re-run as Film, the v1 curve is recognised as Film's own (not the user's), put back, and film-v3 is laid in its place
    // over the same default baseline -- never skipped_user_curve, never v3 stacked on v1.
    MlvPipelineFixture fixture;
    ASSERT_TRUE( openFirstFixture( fixture ) );
    ReceiptSettings &receipt = fixture.receipt();
    const QString base = receipt.gradationCurve();
    ASSERT_TRUE( lookAssistIsDefaultGradationCurve( base ) );
    receipt.setGradationCurve( lookAssistFilmGradationCurveV1( LookAssistScene::Shade ) );
    receipt.setLookAssistBaselineGradationCurve( base );
    QString line;
    {
        FlavorEnv env( "film" );
        ASSERT_TRUE( applyCapturingLine( fixture, receipt, &line ) );
    }
    ASSERT_TRUE( line.endsWith( QStringLiteral(" flavor=film grade=film-v3") ) );
    ASSERT_TRUE( receipt.gradationCurve() == lookAssistFilmGradationCurve( sceneByName( appliedField( line, QStringLiteral("scene") ) ) ) );
    ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve() == base );
}

TEST(LookAssistFilmGrade, AV2ReceiptReRunAsFilmLaysV3)
{
    // A receipt saved under #328: the film-v2 Shade curve laid over the default, the default on record as the baseline.
    // (a) Look Assist off: the v2 curve is Film's own, so the default comes back and the element goes.
    // (b) Re-run as Film: the v2 curve is put back and film-v3 is laid in its place over the same default baseline --
    //     never skipped_user_curve, never v3 stacked on v2.
    for( int off = 1; off >= 0; --off )
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFirstFixture( fixture ) );
        ReceiptSettings &receipt = fixture.receipt();
        const QString base = receipt.gradationCurve();
        ASSERT_TRUE( lookAssistIsDefaultGradationCurve( base ) );
        receipt.setGradationCurve( lookAssistFilmGradationCurveV2( LookAssistScene::Shade ) );
        receipt.setLookAssistBaselineGradationCurve( base );
        QString line;
        FlavorEnv env( "film" );
        if( off )
        {
            receipt.setLookAssistEnabled( false );
            ASSERT_FALSE( applyCapturingLine( fixture, receipt, nullptr ) );
            ASSERT_TRUE( receipt.gradationCurve() == base );
            ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve().isEmpty() );
            continue;
        }
        ASSERT_TRUE( applyCapturingLine( fixture, receipt, &line ) );
        ASSERT_TRUE( line.endsWith( QStringLiteral(" flavor=film grade=film-v3") ) );
        ASSERT_TRUE( receipt.gradationCurve() == lookAssistFilmGradationCurve( sceneByName( appliedField( line, QStringLiteral("scene") ) ) ) );
        ASSERT_TRUE( receipt.gradationCurve() != lookAssistFilmGradationCurveV2( LookAssistScene::Shade ) );
        ASSERT_TRUE( receipt.lookAssistBaselineGradationCurve() == base );
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
