// LOOK-ASSIST-FLAVORS-1: one headless Look Assist run over a tracked fixture frame, as comparable values.
//
// Only API master already had is used (applyHeadlessLookAssist, applyReceipt, renderFrame8), so this header
// compiles against fork/master b5751928 too: the Classic baseline file was dumped from an UNCHANGED master tree
// with it. Tracked fixture clips only.
#ifndef LOOK_ASSIST_FLAVOR_RUN_H
#define LOOK_ASSIST_FLAVOR_RUN_H

#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../src/batch/BatchLogger.h"
#include "../../src/batch/ReceiptApplier.h"

#include <QByteArray>
#include <QCryptographicHash>
#include <QFile>
#include <QString>
#include <QStringList>
#include <QTemporaryDir>

#include <cmath>
#include <vector>

namespace look_flavor_run
{

struct FixtureCase
{
    const char *file;
    int frame;
};

// One frame of the small clip, two of the large one (the deck is in view in both of the large clip's).
inline const std::vector<FixtureCase> &fixtureCases()
{
    static const std::vector<FixtureCase> cases = {
        { "tests/fixtures/clips/tiny_dual_iso.mlv", 0 },
        { "tests/fixtures/clips/large_dual_iso.mlv", 0 },
        { "tests/fixtures/clips/large_dual_iso.mlv", 10 },
    };
    return cases;
}

struct Run
{
    bool ok = false;
    bool applied = false;
    QString key;            // "<clip file name> <frame>"
    QString receipt;        // every slider Look Assist wrote, one line
    QString appliedLine;    // the last "[BATCH] LOOK_ASSIST applied ..." line, newline-free
    QString sha256;         // of the 8-bit frame rendered with that receipt applied
    QString flavorOnReceipt;
    QByteArray log;
    std::vector<uint8_t> rgb;
    int width = 0;
    int height = 0;
    int exposure = 0, contrast = 0, pivot = 0, temperature = 0, tint = 0, vibrance = 0, shadows = 0, highlights = 0;
};

// lookAssist = false renders the clip's own receipt untouched (the "raw" panel of a contact sheet).
// gradeLikeGui = false renders what the headless (CDNG) path produces: the batch applier pushes only the white balance
// of the receipt to the processing object, so contrast / pivot / shadows / highlights / vibrance do not show.
// gradeLikeGui = true also pushes the sliders the way the app's slider handlers do (MainWindow.cpp
// on_horizontalSlider*_valueChanged), so the picture is the one the GUI and a video export show.
inline Run run( const FixtureCase &fixtureCase, bool lookAssist, bool gradeLikeGui = false )
{
    Run out;
    out.key = QStringLiteral("%1 %2").arg( QString::fromLatin1( fixtureCase.file ).section( QLatin1Char('/'), -1 ) )
                                     .arg( fixtureCase.frame );
    MlvPipelineFixture fixture;
    QString error_message;
    if( !fixture.openClipFile( repo_file_path( QString::fromLatin1( fixtureCase.file ) ), &error_message ) ) return out;
    if( !fixture.applyReceipt( &error_message ) ) return out;

    ReceiptSettings &receipt = fixture.receipt();
    if( lookAssist )
    {
        receipt.setLookAssistEnabled( true );
        receipt.setLookAssistBaselineValid( false );
        receipt.setExposure( 0 );
        receipt.setTemperature( -1 );
        receipt.setTint( 0 );

        QTemporaryDir temporary_dir;
        const QString log_path = temporary_dir.filePath( QStringLiteral("look_assist.log") );
        BatchLogger::init( log_path );
        out.applied = ReceiptApplier::applyHeadlessLookAssist(
            &receipt, fixture.video(), fixture.processing(), static_cast<uint32_t>( fixtureCase.frame ) );
        BatchLogger::shutdown();
        QFile log_file( log_path );
        if( log_file.open( QIODevice::ReadOnly | QIODevice::Text ) ) out.log = log_file.readAll();
        for( const QString &line : QString::fromUtf8( out.log ).split( QLatin1Char('\n') ) )
            if( line.contains( QLatin1String( "LOOK_ASSIST applied " ) ) ) out.appliedLine = line.trimmed();
        if( !out.applied ) return out;
        if( !fixture.applyReceipt( &error_message ) ) return out;
    }
#ifdef LOOK_FLAVOR_RUN_HAS_RECEIPT_FLAVOR   // defined by the flavor test; master's ReceiptSettings has no flavor
    out.flavorOnReceipt = receipt.lookAssistFlavor();
#endif
    out.exposure = receipt.exposure();
    out.contrast = receipt.contrast();
    out.pivot = receipt.pivot();
    out.temperature = receipt.temperature();
    out.tint = receipt.tint();
    out.vibrance = receipt.vibrance();
    out.shadows = receipt.shadows();
    out.highlights = receipt.highlights();
    out.receipt = QStringLiteral("exp=%1 contrast=%2 pivot=%3 temp=%4 tint=%5 vibrance=%6 shadows=%7 highlights=%8 chromaSmooth=%9")
        .arg( receipt.exposure() ).arg( receipt.contrast() ).arg( receipt.pivot() ).arg( receipt.temperature() )
        .arg( receipt.tint() ).arg( receipt.vibrance() ).arg( receipt.shadows() ).arg( receipt.highlights() )
        .arg( receipt.chromaSmooth() );

    if( gradeLikeGui )
    {
        processingObject_t *proc = fixture.processing();
        processingSetExposureStops( proc, receipt.exposure() / 100.0 + 1.2 );
        processingSetSimpleContrast( proc, receipt.contrast() / 100.0 );
        processingSetPivot( proc, receipt.pivot() / 100.0 );
        processingSetVibrance( proc, std::pow( ( receipt.vibrance() + 100 ) / 200.0 * 2.0, std::log( 3.6 ) / std::log( 2.0 ) ) );
        processingSetShadows( proc, receipt.shadows() * 1.5 / 100.0 );
        processingSetHighlights( proc, receipt.highlights() * 1.5 / 100.0 );
    }
    resetMlvCache( fixture.video() );
    resetMlvCachedFrame( fixture.video() );
    out.rgb = fixture.renderFrame8( static_cast<uint64_t>( fixtureCase.frame ) );
    out.width = fixture.width();
    out.height = fixture.height();
    out.sha256 = QString::fromLatin1( QCryptographicHash::hash(
        QByteArray( reinterpret_cast<const char *>( out.rgb.data() ), static_cast<int>( out.rgb.size() ) ),
        QCryptographicHash::Sha256 ).toHex() );
    out.ok = !out.rgb.empty();
    return out;
}

} // namespace look_flavor_run

#endif
