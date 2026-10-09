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
    ASSERT_TRUE( r.notice.isEmpty() );
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

// A build that recorded no tree (no generated header) keeps the walk-up and the pin rules, as before, and says nothing.
TEST(RepoRootPin, ABuildThatRecordedNoTreeKeepsTheWalkUp)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString main = makeCheckout( parent, QStringLiteral( "main" ) );
    ASSERT_TRUE( !main.isEmpty() );
    const RepoRootResolution unrecorded = resolve_repo_root( QString(), QString(), main );
    ASSERT_TRUE( unrecorded.refusal.isEmpty() );
    ASSERT_EQ( main.toStdString(), unrecorded.root.toStdString() );
    const RepoRootResolution pinned = resolve_repo_root( main, QString(), QString() );
    ASSERT_TRUE( pinned.notice.isEmpty() );
    ASSERT_TRUE( pinned.refusal.isEmpty() );
    ASSERT_EQ( QDir( main ).absolutePath().toStdString(), pinned.root.toStdString() );
}

// A recorded build tree that is gone (the binary was relocated, or its checkout removed) never leads to a silent root: with
// no pin the checkout above the binary is refused. The removed-lane-checkout shape: the walk-up lands on the main checkout.
TEST(RepoRootPin, AMissingBuildTreeWithoutAPinIsRefused)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString main = makeCheckout( parent, QStringLiteral( "main" ) );
    ASSERT_TRUE( !main.isEmpty() );
    const QString gone = parent.filePath( QStringLiteral( "gone" ) );
    const RepoRootResolution r = resolve_repo_root( QString(), gone, main );
    ASSERT_EQ( ( QStringLiteral( "REPO_ROOT_BUILD_TREE_MISSING: this binary was built from " ) + gone
                 + QStringLiteral( ", which is no longer an MLV-App checkout; the checkout above it is " ) + main
                 + QStringLiteral( "; set MLVAPP_TEST_REPO_ROOT=<checkout> to run a relocated binary explicitly" ) )
                   .toStdString(),
               r.refusal.toStdString() );
    ASSERT_TRUE( r.root.isEmpty() );
}

TEST(RepoRootPin, AMissingBuildTreeWithNoCheckoutAboveIsRefused)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString gone = parent.filePath( QStringLiteral( "gone" ) );
    const RepoRootResolution r = resolve_repo_root( QString(), gone, QString() );
    ASSERT_TRUE( r.refusal.startsWith( QStringLiteral( "REPO_ROOT_BUILD_TREE_MISSING: this binary was built from " ) + gone ) );
    ASSERT_TRUE( r.refusal.contains( QStringLiteral( "the checkout above it is (none);" ) ) );
    ASSERT_TRUE( r.root.isEmpty() );
}

// Moved rather than removed: the recorded directory still exists but is no longer a checkout.
TEST(RepoRootPin, ABuildTreeThatIsNoLongerACheckoutIsRefused)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString main = makeCheckout( parent, QStringLiteral( "main" ) );
    ASSERT_TRUE( !main.isEmpty() );
    ASSERT_TRUE( QDir( parent.path() ).mkpath( QStringLiteral( "moved/src" ) ) );
    const QString moved = parent.filePath( QStringLiteral( "moved" ) );
    const RepoRootResolution r = resolve_repo_root( QString(), moved, main );
    ASSERT_TRUE( r.refusal.startsWith( QStringLiteral( "REPO_ROOT_BUILD_TREE_MISSING: this binary was built from " ) + moved ) );
    ASSERT_TRUE( r.root.isEmpty() );
}

// The explicit pin is the one way to run a relocated binary: it is used, but with a notice naming both trees.
TEST(RepoRootPin, AMissingBuildTreeWithAValidPinUsesThePinWithANotice)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString main = makeCheckout( parent, QStringLiteral( "main" ) );
    ASSERT_TRUE( !main.isEmpty() );
    const QString gone = parent.filePath( QStringLiteral( "gone" ) );
    const QString notice = QStringLiteral( "REPO_ROOT_BUILD_TREE_MISSING: built from " ) + gone
                         + QStringLiteral( " (gone); using pinned " ) + main;
    const RepoRootResolution underMain = resolve_repo_root( main, gone, main );
    ASSERT_TRUE( underMain.refusal.isEmpty() );
    ASSERT_EQ( QDir( main ).absolutePath().toStdString(), underMain.root.toStdString() );
    ASSERT_EQ( notice.toStdString(), underMain.notice.toStdString() );
    const RepoRootResolution outside = resolve_repo_root( main, gone, QString() );
    ASSERT_TRUE( outside.refusal.isEmpty() );
    ASSERT_EQ( QDir( main ).absolutePath().toStdString(), outside.root.toStdString() );
    ASSERT_EQ( notice.toStdString(), outside.notice.toStdString() );
}

TEST(RepoRootPin, AMissingBuildTreeWithAnInvalidPinIsRefusedAsPinInvalid)
{
    QTemporaryDir parent;
    ASSERT_TRUE( parent.isValid() );
    const QString main = makeCheckout( parent, QStringLiteral( "main" ) );
    ASSERT_TRUE( !main.isEmpty() );
    const RepoRootResolution r = resolve_repo_root( parent.path(), parent.filePath( QStringLiteral( "gone" ) ), main );
    ASSERT_TRUE( r.refusal.startsWith( QStringLiteral( "REPO_ROOT_PIN_INVALID: MLVAPP_TEST_REPO_ROOT=" ) ) );
    ASSERT_TRUE( r.notice.isEmpty() );
    ASSERT_TRUE( r.root.isEmpty() );
}
