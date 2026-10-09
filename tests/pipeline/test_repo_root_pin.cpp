// SUITE-RUNNER-REPO-ROOT-PIN-1: the repo root a test reads its fixtures from is the checkout the binary was built from. The
// LOOK-ASSIST-WB-DECISION-1 r2 lane built pipeline_tests in a build dir under the MAIN checkout's run dir from a lane
// worktree's sources; find_repo_root() walked up from the binary to the main checkout and the whole default run read the
// wrong tree (marked INVALID by hand afterwards). resolve_repo_root() is the decision find_repo_root() makes; these drive it on
// real directories.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QDir>
#include <QFile>
#include <QTemporaryDir>

namespace
{

// A directory that passes the checkout test: README.md, receipts, src, tests. Empty when it could not be made.
QString makeCheckout( const QTemporaryDir &parent, const QString &name )
{
    QDir root( parent.path() );
    QFile readme( root.filePath( name + QStringLiteral( "/README.md" ) ) );
    if( !root.mkpath( name + QStringLiteral( "/receipts" ) ) || !root.mkpath( name + QStringLiteral( "/src" ) )
     || !root.mkpath( name + QStringLiteral( "/tests" ) ) || !readme.open( QIODevice::WriteOnly ) )
        return QString();
    readme.close();
    return root.filePath( name );
}

} // namespace

// The binary under test records its own build tree, and that tree is the checkout its fixtures resolve to.
TEST(RepoRootPin, ThisBinaryKnowsTheCheckoutItWasBuiltFrom)
{
    const QString builtFrom = built_from_repo_root();
    ASSERT_TRUE( !builtFrom.isEmpty() );
    ASSERT_TRUE( is_repo_root( builtFrom ) );
    ASSERT_EQ( QDir( builtFrom ).canonicalPath().toLower().toStdString(),
               QDir( find_repo_root() ).canonicalPath().toLower().toStdString() );
}

// The r2 shape: the checkout above the binary is not the one it was built from.
TEST(RepoRootPin, AWalkedUpCheckoutThatIsNotTheBuildTreeIsRefused)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString main = makeCheckout( parent, QStringLiteral( "main" ) );
    ASSERT_TRUE( !main.isEmpty() );
    const QString lane = makeCheckout( parent, QStringLiteral( "lane" ) );
    ASSERT_TRUE( !lane.isEmpty() );
    const RepoRootResolution r = resolve_repo_root( QString(), lane, main );
    ASSERT_TRUE( r.root.isEmpty() );
    ASSERT_TRUE( r.refusal.startsWith( QStringLiteral( "REPO_ROOT_MISMATCH: the checkout above this binary is " ) ) );
    ASSERT_TRUE( r.refusal.contains( QStringLiteral( "set MLVAPP_TEST_REPO_ROOT=" ) + lane ) );
}

TEST(RepoRootPin, AWalkedUpCheckoutThatIsTheBuildTreeIsUsed)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString lane = makeCheckout( parent, QStringLiteral( "lane" ) );
    ASSERT_TRUE( !lane.isEmpty() );
    const RepoRootResolution r = resolve_repo_root( QString(), lane, lane );
    ASSERT_TRUE( r.refusal.isEmpty() );
    ASSERT_EQ( lane.toStdString(), r.root.toStdString() );
}

// The explicit pin is how a build dir under another checkout is run on purpose: it must name the build tree.
TEST(RepoRootPin, APinnedRootThatIsTheBuildTreeWinsOverTheWalkUp)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString main = makeCheckout( parent, QStringLiteral( "main" ) );
    ASSERT_TRUE( !main.isEmpty() );
    const QString lane = makeCheckout( parent, QStringLiteral( "lane" ) );
    ASSERT_TRUE( !lane.isEmpty() );
    const RepoRootResolution r = resolve_repo_root( lane, lane, main );
    ASSERT_TRUE( r.refusal.isEmpty() );
    ASSERT_EQ( QDir( lane ).absolutePath().toStdString(), r.root.toStdString() );
}

TEST(RepoRootPin, APinnedRootThatIsNotTheBuildTreeIsRefused)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString main = makeCheckout( parent, QStringLiteral( "main" ) );
    ASSERT_TRUE( !main.isEmpty() );
    const QString lane = makeCheckout( parent, QStringLiteral( "lane" ) );
    ASSERT_TRUE( !lane.isEmpty() );
    const RepoRootResolution r = resolve_repo_root( main, lane, lane );
    ASSERT_TRUE( r.root.isEmpty() );
    ASSERT_TRUE( r.refusal.startsWith( QStringLiteral( "REPO_ROOT_MISMATCH: MLVAPP_TEST_REPO_ROOT=" ) + main ) );
}

TEST(RepoRootPin, APinnedRootThatIsNotACheckoutIsRefused)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString lane = makeCheckout( parent, QStringLiteral( "lane" ) );
    ASSERT_TRUE( !lane.isEmpty() );
    const RepoRootResolution r = resolve_repo_root( parent.path(), lane, lane );
    ASSERT_TRUE( r.root.isEmpty() );
    ASSERT_TRUE( r.refusal.startsWith( QStringLiteral( "REPO_ROOT_PIN_INVALID: MLVAPP_TEST_REPO_ROOT=" ) ) );
}

// A binary whose build tree is gone (relocated) and a build that recorded none keep the walk-up, as before.
TEST(RepoRootPin, ABuildTreeThatIsNoLongerACheckoutKeepsTheWalkUp)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString main = makeCheckout( parent, QStringLiteral( "main" ) );
    ASSERT_TRUE( !main.isEmpty() );
    const RepoRootResolution gone = resolve_repo_root( QString(), parent.filePath( QStringLiteral( "gone" ) ), main );
    ASSERT_TRUE( gone.refusal.isEmpty() );
    ASSERT_EQ( main.toStdString(), gone.root.toStdString() );
    const RepoRootResolution unrecorded = resolve_repo_root( QString(), QString(), main );
    ASSERT_TRUE( unrecorded.refusal.isEmpty() );
    ASSERT_EQ( main.toStdString(), unrecorded.root.toStdString() );
}
