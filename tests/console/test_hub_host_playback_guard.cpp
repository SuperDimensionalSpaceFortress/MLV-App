// HUB-VM-NO-LOCAL-PLAYBACK-1 (owner objection 2026-10-11): the app's automation playback entries refuse to start on the hub
// VM (Virtual-Ten, a guest on Ultra-Magnus). RED fixtures per surface:
//   1. the pure decision (HubHostPlaybackGuard.h): hub host refused, venue host / developer machine allowed, the CI-fixture
//      override honoured only in its exact spelling;
//   2. the wiring in main.cpp (needs a full GUI build, so the source is read as text, as the other *_wiring tests do): the
//      refusal sits in main() BEFORE the run-scoped settings store and before any window, at the top of both automation
//      entry functions, and never in the normal-GUI path.
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
    ASSERT_FALSE(hub_host_playback_guard::refuses(QStringLiteral("VIRTUAL-TEN"), QByteArray(" CI_FIXTURE_ONLY ")));
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

    const int refuseAt = mainBody.indexOf(QStringLiteral("if (automationRun && hubHostPlaybackRefused())"));
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
    ASSERT_TRUE(mainBody.mid(refuseAt, 220).contains(QStringLiteral("kRefusalExitCode")));
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

TEST(HubHostPlaybackGuard, TheNormalInteractivePathNeverTouchesTheGuard)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    const int normalGuiAt = source.indexOf(QStringLiteral("/* Normal GUI mode"));
    ASSERT_TRUE(normalGuiAt >= 0);
    ASSERT_FALSE(source.mid(normalGuiAt).contains(QStringLiteral("hubHostPlaybackRefused")));
    // And only the three call sites exist: main(), runPlaybackProfile(), runGuiPlaybackSmoke().
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
