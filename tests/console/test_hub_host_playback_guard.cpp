// HUB-VM-NO-LOCAL-PLAYBACK-1 (owner objection 2026-10-11): no MLVApp GUI or playback start on the hub VM (Virtual-Ten, a
// guest on Ultra-Magnus) -- since r3 the ordinary interactive launch included. RED fixtures per surface:
//   1. the pure decision (HubHostPlaybackGuard.h): hub host refused, venue host / developer machine allowed, the CI-fixture
//      override honoured only in its exact, untrimmed spelling; every start that constructs a MainWindow is guarded, only
//      --batch / --trim-mlv without an automation request are not;
//   2. the wiring in main.cpp (needs a full GUI build, so the source is read as text, as the other *_wiring tests do): the
//      refusal sits in main() BEFORE the run-scoped settings store and before any window, covers the ordinary launch, and
//      sits at the top of both automation entry functions.
// Nothing here launches the app.
// The host list is pinned to tools/profiling/dual-venue/hub-hosts.json by tools/repo_hygiene/test_hub_vm_no_local_playback.py.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QByteArray>
#include <QFile>
#include <QString>
#include <QStringList>
#include <QTextStream>

#include "../../platform/qt/HubHostPlaybackGuard.h"

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

QByteArray noOverride() { return QByteArray(); }
QByteArray fixtureOverride() { return QByteArray("CI_FIXTURE_ONLY"); }

} // namespace

TEST(HubHostPlaybackGuard, TheHubVmIsRefusedAnyCase)
{
    ASSERT_TRUE(hub_host_playback_guard::refuses(QStringLiteral("VIRTUAL-TEN"), noOverride()));
    ASSERT_TRUE(hub_host_playback_guard::refuses(QStringLiteral("virtual-ten"), noOverride()));
    ASSERT_TRUE(hub_host_playback_guard::refuses(QStringLiteral("  Virtual-Ten \r\n"), noOverride()));
}

TEST(HubHostPlaybackGuard, VenuesAndOtherMachinesAreNotRefused)
{
    ASSERT_FALSE(hub_host_playback_guard::refuses(QStringLiteral("BACHELOR"), noOverride()));
    ASSERT_FALSE(hub_host_playback_guard::refuses(QStringLiteral("ULTRA-MAGNUS"), noOverride()));
    ASSERT_FALSE(hub_host_playback_guard::refuses(QStringLiteral("DESKTOP-ABC123"), noOverride()));
    ASSERT_FALSE(hub_host_playback_guard::refuses(QString(), noOverride()));
    // Whole-name match: a name that merely contains the hub's is not the hub.
    ASSERT_FALSE(hub_host_playback_guard::refuses(QStringLiteral("VIRTUAL-TEN-2"), noOverride()));
    ASSERT_FALSE(hub_host_playback_guard::refuses(QStringLiteral("MY-VIRTUAL-TEN"), noOverride()));
}

TEST(HubHostPlaybackGuard, OnlyTheExactFixtureOverrideLiftsTheRefusal)
{
    ASSERT_FALSE(hub_host_playback_guard::refuses(QStringLiteral("VIRTUAL-TEN"), fixtureOverride()));
    // Untrimmed, as the launcher guards (-cne) and the lane lint (-ceq) compare: a padded value is not the override.
    ASSERT_TRUE(hub_host_playback_guard::refuses(QStringLiteral("VIRTUAL-TEN"), QByteArray(" CI_FIXTURE_ONLY ")));
    ASSERT_TRUE(hub_host_playback_guard::refuses(QStringLiteral("VIRTUAL-TEN"), QByteArray("CI_FIXTURE_ONLY\n")));
    // A truthy-looking value is NOT the override (an inherited MLVAPP_*=1 habit must not open the guard).
    ASSERT_TRUE(hub_host_playback_guard::refuses(QStringLiteral("VIRTUAL-TEN"), QByteArray("1")));
    ASSERT_TRUE(hub_host_playback_guard::refuses(QStringLiteral("VIRTUAL-TEN"), QByteArray("true")));
    ASSERT_TRUE(hub_host_playback_guard::refuses(QStringLiteral("VIRTUAL-TEN"), QByteArray("ci_fixture_only")));
}

TEST(HubHostPlaybackGuard, TheRefusalIsTypedAndNamesTheVenueRoute)
{
    const QString message = hub_host_playback_guard::refusalMessage(QStringLiteral("VIRTUAL-TEN"));
    ASSERT_TRUE(message.contains(QStringLiteral("HUB_VM_LOCAL_PLAYBACK_REFUSED")));
    ASSERT_TRUE(message.contains(QStringLiteral("VIRTUAL-TEN")));
    ASSERT_TRUE(message.contains(QStringLiteral("venue")));
    ASSERT_NE(0, hub_host_playback_guard::kRefusalExitCode);
}

TEST(HubHostPlaybackGuard, MainRefusesBeforeTheSettingsStoreAndBeforeAnyWindow)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    const int mainAt = source.indexOf(QStringLiteral("int main(int argc, char *argv[])"));
    ASSERT_TRUE(mainAt >= 0);
    const QString mainBody = source.mid(mainAt);

    const int refuseAt = mainBody.indexOf(QStringLiteral(
        "if (hub_host_playback_guard::startIsGuarded(hasBatchFlag(argc, argv), hasTrimMlvFlag(argc, argv), automationRun)"));
    const int isolateAt = mainBody.indexOf(QStringLiteral("automation_settings::isolate("));
    const int appAt = mainBody.indexOf(QStringLiteral("MyApplication a(argc, argv);"));
    const int normalGuiAt = mainBody.indexOf(QStringLiteral("/* Normal GUI mode"));
    ASSERT_TRUE(refuseAt >= 0);
    ASSERT_TRUE(isolateAt > refuseAt);
    ASSERT_TRUE(appAt > refuseAt);
    ASSERT_TRUE(normalGuiAt > refuseAt);
    // The refusal covers every automation entry main() recognises: the smoke flag, the profile flag, the autoplay hook.
    const QString definition = mainBody.left(isolateAt);
    ASSERT_TRUE(definition.contains(QStringLiteral("hasGuiPlaybackSmokeFlag(argc, argv)")));
    ASSERT_TRUE(definition.contains(QStringLiteral("hasPlaybackProfileFlag(argc, argv)")));
    ASSERT_TRUE(definition.contains(QStringLiteral("MLVAPP_AUTOPLAY_SECONDS")));
    const QString condition = mainBody.mid(refuseAt, 260);
    ASSERT_TRUE(condition.contains(QStringLiteral("&& hubHostPlaybackRefused())")));
    ASSERT_TRUE(condition.contains(QStringLiteral("kRefusalExitCode")));
}

TEST(HubHostPlaybackGuard, EachAutomationEntryFunctionRefusesFirst)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    const QStringList signatures = {
        QStringLiteral("static int runPlaybackProfile(QApplication &app)"),
        QStringLiteral("static int runGuiPlaybackSmoke(QApplication &app)"),
    };
    for (const QString & signature : signatures)
    {
        const int at = source.indexOf(signature);
        ASSERT_TRUE(at >= 0);
        // The very first statement of the function body.
        const int bodyAt = source.indexOf(QLatin1Char('{'), at);
        ASSERT_TRUE(bodyAt > at);
        const QString firstLines = source.mid(bodyAt, 200);
        ASSERT_TRUE(firstLines.contains(QStringLiteral("if (hubHostPlaybackRefused()) return hub_host_playback_guard::kRefusalExitCode;")));
    }
}

// r3 (hub ruling on sol's r1 BLOCKER): an ordinary launch -- no flag, no MLVAPP_AUTOPLAY_SECONDS -- on a hub host is
// refused; the headless entries that never construct a MainWindow are not guarded. RED on b3d9dd8c, whose decision was
// "automationRun && refused", so the ordinary launch skipped the check and reached MainWindow w / w.show().
TEST(HubHostPlaybackGuard, TheOrdinaryHubLaunchIsRefused)
{
    using hub_host_playback_guard::refuses;
    using hub_host_playback_guard::startIsGuarded;
    const bool batch = true, trimMlv = true, automation = true;

    // The ordinary GUI start (the bare executable, with or without a file argument): guarded, and refused on the hub.
    ASSERT_TRUE(startIsGuarded(!batch, !trimMlv, !automation));
    ASSERT_TRUE(startIsGuarded(!batch, !trimMlv, !automation) && refuses(QStringLiteral("VIRTUAL-TEN"), noOverride()));
    // The same start on a venue or a user machine is not refused.
    ASSERT_FALSE(startIsGuarded(!batch, !trimMlv, !automation) && refuses(QStringLiteral("BACHELOR"), noOverride()));
    ASSERT_FALSE(startIsGuarded(!batch, !trimMlv, !automation) && refuses(QStringLiteral("ULTRA-MAGNUS"), noOverride()));
    ASSERT_FALSE(startIsGuarded(!batch, !trimMlv, !automation) && refuses(QStringLiteral("DESKTOP-ABC123"), noOverride()));
    // The automation entries stay guarded.
    ASSERT_TRUE(startIsGuarded(!batch, !trimMlv, automation));
    // Headless entries that never construct a MainWindow are not guarded (batch CDNG CLI incl. --batch --help, --trim-mlv)...
    ASSERT_FALSE(startIsGuarded(batch, !trimMlv, !automation));
    ASSERT_FALSE(startIsGuarded(!batch, trimMlv, !automation));
    // ...unless they carry an automation request, which stays refused on the hub exactly as in r1/r2.
    ASSERT_TRUE(startIsGuarded(batch, !trimMlv, automation));
    ASSERT_TRUE(startIsGuarded(!batch, trimMlv, automation));

    // Wiring: main() no longer gates the check on automationRun alone, and the guarded check precedes the normal-GUI window.
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    const int mainAt = source.indexOf(QStringLiteral("int main(int argc, char *argv[])"));
    ASSERT_TRUE(mainAt >= 0);
    const QString mainBody = source.mid(mainAt);
    ASSERT_FALSE(mainBody.contains(QStringLiteral("if (automationRun && hubHostPlaybackRefused())")));
    const int guardAt = mainBody.indexOf(QStringLiteral("startIsGuarded(hasBatchFlag(argc, argv), hasTrimMlvFlag(argc, argv), automationRun)"));
    const int windowAt = mainBody.indexOf(QStringLiteral("MainWindow w(argc, argv);"));
    const int showAt = mainBody.indexOf(QStringLiteral("w.show();"));
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(windowAt > guardAt);
    ASSERT_TRUE(showAt > windowAt);
}

TEST(HubHostPlaybackGuard, TheGuardHasExactlyThreeCallSites)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    // Only the three call sites exist: main() (covers the normal GUI too), runPlaybackProfile(), runGuiPlaybackSmoke().
    int calls = 0;
    int from = 0;
    const QString needle = QStringLiteral("hubHostPlaybackRefused()");
    while (true)
    {
        const int at = source.indexOf(needle, from);
        if (at < 0) break;
        ++calls;
        from = at + needle.length();
    }
    ASSERT_EQ(4, calls);   // the definition + the three call sites
}
