#ifndef TESTS_COMMON_REPO_PATHS_H
#define TESTS_COMMON_REPO_PATHS_H

#include <QString>

QString find_repo_root();
QString repo_file_path(const QString & relative_path);

// SUITE-RUNNER-REPO-ROOT-PIN-1. find_repo_root() resolves the checkout a test reads its fixtures from, in order:
// MLVAPP_TEST_REPO_ROOT (an explicit pin, e.g. tools/testing/run-windows-test.ps1 -RepoRoot), else the first checkout above
// the binary. Either one is REFUSED (a typed line on stderr, exit kRepoRootRefusedExitCode) when it is not the checkout the
// binary was built from: a build dir under another checkout's run dir used to resolve to that other checkout silently.
// When the recorded build tree is no longer a checkout (a relocated binary), only an explicit pin may choose the root: the
// walk-up is refused (REPO_ROOT_BUILD_TREE_MISSING) and a valid pin is used with a one-line NOTICE on stderr per process.
const int kRepoRootRefusedExitCode = 86;

struct RepoRootResolution
{
    QString root;      // empty when no checkout was found (unchanged behaviour)
    QString refusal;   // non-empty = refused; the typed message, starting REPO_ROOT_PIN_INVALID, REPO_ROOT_MISMATCH or
                       // REPO_ROOT_BUILD_TREE_MISSING
    QString notice;    // non-empty = accepted, but not silently: a pin used for a binary whose recorded build tree is gone
};

// README.md, receipts, src and tests all present.
bool is_repo_root(const QString & dir);
// The checkout this binary was compiled from (qmake writes it, tests/common/mlvapp_test_source_root.h.in); empty when the
// build did not record one.
QString built_from_repo_root();
// The decision find_repo_root() makes, pure. An empty built_from (a build that recorded none) keeps the pin rules and the
// walk-up as before.
RepoRootResolution resolve_repo_root(const QString & pinned, const QString & built_from, const QString & walked_up);

#endif // TESTS_COMMON_REPO_PATHS_H
