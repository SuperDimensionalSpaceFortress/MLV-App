// LOOK-PRESET-DROPDOWN-1: the windowed look dropdown. Owner, 2026-10-15: "User should be able to select from our Look
// Presets via a dropdown when in windowed mode and have it change on the fly. The dropdown should have our Look Name
// without cropping". The entries and the pick-to-state mapping are pure (LookPresetDropdownPolicy.h); the MainWindow
// wiring is GUI code not linked into console_tests, so it is pinned as source text. The no-elide width check at
// 100 / 150 / 200 % needs a real QComboBox and lives in gui_tests (lookPresetDropdownNeverElidesAt100150And200PercentScaling).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/LookPresetDropdownPolicy.h"

#include <QFile>
#include <QSet>
#include <QString>
#include <QTextStream>

#include <string>

using namespace lookassist;

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

// The body of one function, from its definition to its closing brace in column 0.
QString functionBody(const QString &source, const QString &signature)
{
    const int start = source.indexOf(signature);
    ASSERT_TRUE(start >= 0);
    if ( start < 0 ) return QString();
    const int end = source.indexOf(QStringLiteral("\n}\n"), start);
    return source.mid(start, end > start ? end - start : -1);
}

} // namespace

TEST(LookPresetDropdown, TheRegistryListsEveryFlavorOnceAndTheSelectorAcceptsExactlyThose)
{
    const std::vector<LookAssistFlavor> &flavors = lookAssistFlavors();
    ASSERT_EQ(4, static_cast<int>(flavors.size()));
    QSet<QString> names;
    for ( const LookAssistFlavor flavor : flavors )
    {
        const QString name = lookAssistFlavorName(flavor);
        ASSERT_FALSE(names.contains(name));
        names.insert(name);
        // Every registry name selects its own flavor, from any layer, case-insensitively.
        const LookAssistFlavorSelection app = lookAssistSelectFlavor(QString(), QString(), name.toUpper());
        ASSERT_FALSE(app.unknownValue);
        ASSERT_TRUE(app.flavor == flavor);
        ASSERT_TRUE(lookAssistSelectFlavor(name, QString(), QString()).flavor == flavor);
    }
    // A name outside the registry is never selected.
    ASSERT_TRUE(lookAssistSelectFlavor(QString(), QString(), QStringLiteral("off")).unknownValue);
    ASSERT_TRUE(lookAssistSelectFlavor(QString(), QString(), QStringLiteral("vision3")).unknownValue);
}

TEST(LookPresetDropdown, EntriesAreOffThenTheRegistryUnderTheLabelsNames)
{
    const QVector<look_preset_dropdown::Entry> entries = look_preset_dropdown::entries();
    const std::vector<LookAssistFlavor> &flavors = lookAssistFlavors();
    ASSERT_EQ(static_cast<int>(flavors.size()) + 1, entries.size());
    ASSERT_EQ(std::string("off"), entries.at(0).key.toStdString());
    ASSERT_EQ(std::string("Off"), entries.at(0).displayName.toStdString());
    for ( int i = 0; i < static_cast<int>(flavors.size()); ++i )
    {
        const QString name = lookAssistFlavorName(flavors.at(static_cast<size_t>(i)));
        ASSERT_EQ(name.toStdString(), entries.at(i + 1).key.toStdString());
        // The same user-facing name the look label shows ("Look: <name>").
        ASSERT_EQ(look_label::flavorDisplayName(name).toStdString(), entries.at(i + 1).displayName.toStdString());
    }
    ASSERT_EQ(std::string("Classic"), entries.at(1).displayName.toStdString());
    ASSERT_EQ(std::string("Cinematic"), entries.at(2).displayName.toStdString());
    ASSERT_EQ(std::string("Film grade"), entries.at(3).displayName.toStdString());
    ASSERT_EQ(std::string("Kodak Vision3 50D"), entries.at(4).displayName.toStdString());
    ASSERT_EQ(std::string("Kodak Vision3 50D"), look_preset_dropdown::longestDisplayName().toStdString());
}

TEST(LookPresetDropdown, TheListIsBuiltFromTheRegistryNotACopy)
{
    // No flavor spelling in the dropdown's own code: the names come from lookAssistFlavors() / lookAssistFlavorName, so a
    // new flavor added to the registry appears in the dropdown with no edit here.
    const QString policy = readRepoFile(QStringLiteral("platform/qt/LookPresetDropdownPolicy.h"));
    ASSERT_TRUE(policy.contains(QStringLiteral("lookassist::lookAssistFlavors()")));
    const char *spellings[] = { "\"classic\"", "\"cinematic\"", "\"film\"", "\"vision50d\"", "\"Kodak" };
    for ( const char *spelling : spellings ) ASSERT_FALSE(policy.contains(QString::fromLatin1(spelling)));
    const QString widget = readRepoFile(QStringLiteral("platform/qt/LookPresetDropdown.cpp"));
    ASSERT_TRUE(widget.contains(QStringLiteral("for( const Entry &entry : entries() ) combo->addItem( entry.displayName, entry.key );")));
    for ( const char *spelling : spellings ) ASSERT_FALSE(widget.contains(QString::fromLatin1(spelling)));
}

TEST(LookPresetDropdown, EachEntryMapsToTheRightState)
{
    const look_preset_dropdown::Selection off = look_preset_dropdown::selectionFor(QStringLiteral("off"));
    ASSERT_TRUE(off.valid);
    ASSERT_FALSE(off.lookAssistEnabled);
    ASSERT_TRUE(off.flavorName.isEmpty());
    for ( const LookAssistFlavor flavor : lookAssistFlavors() )
    {
        const QString name = lookAssistFlavorName(flavor);
        const look_preset_dropdown::Selection pick = look_preset_dropdown::selectionFor(name);
        ASSERT_TRUE(pick.valid);
        ASSERT_TRUE(pick.lookAssistEnabled);
        ASSERT_EQ(name.toStdString(), pick.flavorName.toStdString());
        // The flavor the grade then uses is exactly the picked one.
        ASSERT_TRUE(lookAssistSelectFlavor(QString(), QString(), pick.flavorName).flavor == flavor);
    }
    ASSERT_TRUE(look_preset_dropdown::selectionFor(QStringLiteral(" Vision50D ")).valid);
    ASSERT_FALSE(look_preset_dropdown::selectionFor(QStringLiteral("mystery")).valid);
    ASSERT_FALSE(look_preset_dropdown::selectionFor(QString()).valid);
}

TEST(LookPresetDropdown, StateMapsBackToTheEntryThatShowsIt)
{
    const QVector<look_preset_dropdown::Entry> entries = look_preset_dropdown::entries();
    for ( const LookAssistFlavor flavor : lookAssistFlavors() )
    {
        const QString name = lookAssistFlavorName(flavor);
        const int index = look_preset_dropdown::indexFor(true, name);
        ASSERT_TRUE(index > 0);
        ASSERT_EQ(name.toStdString(), entries.at(index).key.toStdString());
        // Off whatever the flavor, while Look Assist is off.
        ASSERT_EQ(0, look_preset_dropdown::indexFor(false, name));
        // Round trip: pick -> state -> the same entry.
        const look_preset_dropdown::Selection pick = look_preset_dropdown::selectionFor(entries.at(index).key);
        ASSERT_EQ(index, look_preset_dropdown::indexFor(pick.lookAssistEnabled, pick.flavorName));
    }
    ASSERT_EQ(-1, look_preset_dropdown::indexFor(true, QStringLiteral("mystery")));
}

TEST(LookPresetDropdownWiring, OneControlSyncedBothWays)
{
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    // One control: the dropdown replaced the parity lane's windowed label.
    ASSERT_FALSE(mw.contains(QStringLiteral("lookLabelStatus")));
    ASSERT_TRUE(mw.contains(QStringLiteral("m_pLookPresetDropdown->setObjectName( QStringLiteral( \"lookPresetDropdown\" ) );")));
    ASSERT_TRUE(mw.contains(QStringLiteral("look_preset_dropdown::populate( m_pLookPresetDropdown );")));
    // A user's pick only: activated(), never currentIndexChanged (a sync must not apply a look).
    ASSERT_TRUE(mw.contains(QStringLiteral("connect( m_pLookPresetDropdown, QOverload<int>::of( &QComboBox::activated ),")));

    // State -> dropdown: every change path ends in updateLookLabel (pinned in test_look_label_policy), which syncs the
    // dropdown, signals blocked, and never writes the grade.
    const QString update = functionBody(mw, QStringLiteral("void MainWindow::updateLookLabel( void )"));
    ASSERT_TRUE(update.contains(QStringLiteral("syncLookPresetDropdown( lookOn, flavorName );")));
    const QString sync = functionBody(mw, QStringLiteral("void MainWindow::syncLookPresetDropdown( bool lookOn, const QString &flavorName )"));
    ASSERT_TRUE(sync.contains(QStringLiteral("const QSignalBlocker blocker( m_pLookPresetDropdown );")));
    ASSERT_TRUE(sync.contains(QStringLiteral("look_preset_dropdown::indexFor( lookOn, flavorName )")));
    ASSERT_FALSE(sync.contains(QStringLiteral("selectLookPreset")));
    ASSERT_FALSE(sync.contains(QStringLiteral("->setChecked(")));
    ASSERT_FALSE(sync.contains(QStringLiteral("->setLookAssist")));

    // Dropdown -> state: through the Look Assist panel's own paths (the flavor selector, the persisted app setting, and
    // the clip's switch), so the menu, settings, receipts and the fullscreen label all follow.
    const QString activated = functionBody(mw, QStringLiteral("void MainWindow::onLookPresetDropdownActivated( int index )"));
    ASSERT_TRUE(activated.contains(QStringLiteral("selectLookPreset( m_pLookPresetDropdown->itemData( index ).toString(), \"dropdown\" );")));
    const QString select = functionBody(mw, QStringLiteral("bool MainWindow::selectLookPreset( const QString &key, const char *source )"));
    ASSERT_TRUE(select.contains(QStringLiteral("look_preset_dropdown::selectionFor( key )")));
    ASSERT_TRUE(select.contains(QStringLiteral("ui->comboBoxLookAssistFlavor->setCurrentIndex( flavorIndex );")));
    ASSERT_TRUE(select.contains(QStringLiteral("on_checkBoxLookAssistEnable_clicked( true );")));
    ASSERT_TRUE(select.contains(QStringLiteral("on_checkBoxLookAssistEnable_clicked( false );")));
    ASSERT_TRUE(select.contains(QStringLiteral("updateLookLabel();")));
    // Never stops or restarts playback.
    ASSERT_FALSE(select.contains(QStringLiteral("actionPlay->setChecked")));
    ASSERT_FALSE(select.contains(QStringLiteral("on_actionPlay_triggered")));

    // The windowed look the receipt reports is read from the dropdown itself.
    const QString shown = functionBody(mw, QStringLiteral("QString MainWindow::lookLabelShownText( QString *surface )"));
    ASSERT_TRUE(shown.contains(QStringLiteral("m_pLookPresetDropdown->currentIndex()")));
    ASSERT_TRUE(shown.contains(QStringLiteral("where = QStringLiteral(\"windowed_status_bar\");")));
}

TEST(LookPresetDropdownWiring, PersistsThroughTheExistingLookAssistSetting)
{
    // No new key: the flavor is the selector's persisted app setting, and Look Assist on/off is the clip's receipt switch.
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(mw.contains(QStringLiteral("set.setValue( \"lookAssistFlavor\", ui->comboBoxLookAssistFlavor->currentData().toString() );")));
    ASSERT_TRUE(mw.contains(QStringLiteral("receipt->setLookAssistEnabled( ui->checkBoxLookAssistEnable->isChecked() );")));
    const QString select = functionBody(mw, QStringLiteral("bool MainWindow::selectLookPreset( const QString &key, const char *source )"));
    ASSERT_FALSE(select.contains(QStringLiteral("setValue(")));
}

TEST(LookPresetDropdownWiring, AFlavorPickOverALandedAnalysisIsLaidWithoutAnalysingAgain)
{
    // On the fly: r2's venue capture measured a ~4.5 s GUI-thread stall mid-Play when a flavor change re-ran the whole
    // analysis (night clip). The scene and balance are flavor-blind, so a landed analysis's Classic preset is kept and
    // another flavor is laid over it -- no picture rendered, no analysis.
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString slot = functionBody(mw, QStringLiteral("void MainWindow::on_comboBoxLookAssistFlavor_currentIndexChanged( int )"));
    const int relayAt = slot.indexOf(QStringLiteral("if( relayLookAssistFlavor( ACTIVE_RECEIPT, currentLookAssistFlavor() ) ) return;"));
    const int analyseAt = slot.indexOf(QStringLiteral("m_lookAssistApplied.flavorChanged("));
    ASSERT_TRUE(relayAt >= 0 && analyseAt > relayAt);   // the full analysis stays the fallback

    const QString relay = functionBody(mw, QStringLiteral("bool MainWindow::relayLookAssistFlavor( ReceiptSettings *receipt, LookAssistFlavor flavor )"));
    // Only over THIS clip's landed analysis, with Look Assist on.
    ASSERT_TRUE(relay.contains(QStringLiteral("m_lookAssistFlavorRelay.receipt != receipt")));
    ASSERT_TRUE(relay.contains(QStringLiteral("!m_lookAssistApplied.isApplied( receipt )")));
    ASSERT_TRUE(relay.contains(QStringLiteral("!receipt->lookAssistEnabled()")));
    // The analysis's own overlay: its Classic preset, the shared flavor-delta function, the grade-curve swap.
    ASSERT_TRUE(relay.contains(QStringLiteral("LookAssistPreset preset = m_lookAssistFlavorRelay.classicPreset;")));
    ASSERT_TRUE(relay.contains(QStringLiteral("lookAssistApplyFlavorDeltas( &preset, scene, flavor );")));
    ASSERT_TRUE(relay.contains(QStringLiteral("restoreLookAssistBaselineGradationCurve( receipt );")));
    ASSERT_TRUE(relay.contains(QStringLiteral("applyLookAssistFilmGrade( receipt, scene, flavor )")));
    ASSERT_TRUE(relay.contains(QStringLiteral("m_lookAssistFlavorOutcome.landed( receipt, lookAssistFlavorName( flavor ), false );")));
    // Nothing rendered or analysed, and the flavor-blind sliders are left alone.
    const char *never[] = { "applyLookAssistToReceipt", "get_area_average_downscale_thumnail", "processedThumbnail",
                            "on_checkBoxLookAssistEnable_clicked", "setExposure(", "setTemperature(", "setTint(",
                            "actionPlay->setChecked" };
    for ( const char *call : never ) ASSERT_FALSE(relay.contains(QString::fromLatin1(call)));

    // Recorded only where the sync analysis LANDS (after the safety-fallback return), from the preset before the flavor;
    // forgotten when an analysis starts and when Look Assist goes off.
    const int landedAt = mw.indexOf(QStringLiteral("m_lookAssistFlavorRelay.classicPreset = presetBeforeFlavor;"));
    const int fallbackAt = mw.indexOf(QStringLiteral("requestFrameRefresh( true, \"look-assist-safety-fallback\" );"));
    ASSERT_TRUE(fallbackAt >= 0 && landedAt > fallbackAt);
    ASSERT_EQ(3, mw.count(QStringLiteral("presetBeforeFlavor = preset;")));
    ASSERT_TRUE(mw.contains(QStringLiteral("m_lookAssistFlavorOutcome.begin();   // reported (telemetry, receipt) only once this analysis lands\n"
                                           "    m_lookAssistFlavorRelay = LookAssistFlavorRelay();")));
    const QString toggle = functionBody(mw, QStringLiteral("void MainWindow::on_checkBoxLookAssistEnable_clicked( bool checked )"));
    ASSERT_TRUE(toggle.contains(QStringLiteral("m_lookAssistFlavorRelay = LookAssistFlavorRelay();")));
}

TEST(LookPresetDropdownWiring, ARePickDuringPlayAnalysesTheFrameOnScreen)
{
    // On the fly during playback: the toggle's re-analysis must not wait for the baseline frame (it never comes back while
    // playing); in every quality mode it takes the frame presented after the baseline restore, and Play continues.
    const QString mw = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString settled = functionBody(mw, QStringLiteral("bool MainWindow::isLookAssistFrameSettledForAnalysis( int baselineFrame,"));
    const int playAt = settled.indexOf(QStringLiteral("if( ui->actionPlay->isChecked() )"));
    const int equalAt = settled.indexOf(QStringLiteral("return static_cast<int>( m_lastPresentedRequestContext.frameNumber ) == baselineFrame;"));
    ASSERT_TRUE(playAt >= 0 && equalAt > playAt);
    ASSERT_TRUE(settled.indexOf(QStringLiteral("*analysisFrame = static_cast<int>( m_lastPresentedRequestContext.frameNumber );"), playAt) > playAt);
    // The serial floor and the presentation generation still gate it (a frame from before the restore is never analysed).
    ASSERT_TRUE(settled.indexOf(QStringLiteral("if( m_lastPresentedRequestSerial < requestSerialFloor ) return false;")) < playAt);
}
