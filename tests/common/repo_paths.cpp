#include "repo_paths.h"

#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>

#include <cstdio>
#include <cstdlib>

// Written by qmake (tests/common/test_defaults.pri); a build that does not generate it records no built-from tree.
#if defined(__has_include)
#if __has_include("mlvapp_test_source_root.h")
#include "mlvapp_test_source_root.h"
#endif
#endif

namespace
{

QString walk_up_repo_root()
{
    QDir probe(QCoreApplication::applicationDirPath());
    for (int depth = 0; depth < 8; ++depth) {
        if (is_repo_root(probe.absolutePath())) {
            return probe.absolutePath();
        }
        if (!probe.cdUp()) {
            break;
        }
    }
    return QString();
}

bool same_directory(const QString & a, const QString & b)
{
    const QString canonical_a = QFileInfo(a).canonicalFilePath();
    const QString canonical_b = QFileInfo(b).canonicalFilePath();
    const QString left = canonical_a.isEmpty() ? QDir::cleanPath(QDir(a).absolutePath()) : canonical_a;
    const QString right = canonical_b.isEmpty() ? QDir::cleanPath(QDir(b).absolutePath()) : canonical_b;
#ifdef Q_OS_WIN
    return left.compare(right, Qt::CaseInsensitive) == 0;
#else
    return left == right;
#endif
}

} // namespace

bool is_repo_root(const QString & dir)
{
    if (dir.isEmpty()) {
        return false;
    }
    const QDir probe(dir);
    return QFileInfo::exists(probe.filePath(QStringLiteral("README.md"))) &&
           QFileInfo::exists(probe.filePath(QStringLiteral("receipts"))) &&
           QFileInfo::exists(probe.filePath(QStringLiteral("src"))) &&
           QFileInfo::exists(probe.filePath(QStringLiteral("tests")));
}

QString built_from_repo_root()
{
#ifdef MLVAPP_TEST_SOURCE_ROOT
    return QString::fromUtf8(MLVAPP_TEST_SOURCE_ROOT);
#else
    return QString();
#endif
}

RepoRootResolution resolve_repo_root(const QString & pinned, const QString & built_from, const QString & walked_up)
{
    RepoRootResolution resolution;
    const bool built_from_known = is_repo_root(built_from);
    if (!pinned.isEmpty()) {
        if (!is_repo_root(pinned)) {
            resolution.refusal = QStringLiteral("REPO_ROOT_PIN_INVALID: MLVAPP_TEST_REPO_ROOT=%1 is not an MLV-App checkout "
                                                "(README.md, receipts, src and tests)").arg(pinned);
            return resolution;
        }
        if (built_from_known && !same_directory(pinned, built_from)) {
            resolution.refusal = QStringLiteral("REPO_ROOT_MISMATCH: MLVAPP_TEST_REPO_ROOT=%1 but this binary was built from %2")
                                     .arg(pinned, built_from);
            return resolution;
        }
        resolution.root = QDir(pinned).absolutePath();
        return resolution;
    }
    if (!walked_up.isEmpty() && built_from_known && !same_directory(walked_up, built_from)) {
        resolution.refusal = QStringLiteral("REPO_ROOT_MISMATCH: the checkout above this binary is %1 but it was built from %2; "
                                            "set MLVAPP_TEST_REPO_ROOT=%2 to run it from here")
                                 .arg(walked_up, built_from);
        return resolution;
    }
    resolution.root = walked_up;
    return resolution;
}

QString find_repo_root()
{
    const RepoRootResolution resolution = resolve_repo_root(
        qEnvironmentVariable("MLVAPP_TEST_REPO_ROOT"), built_from_repo_root(), walk_up_repo_root());
    if (!resolution.refusal.isEmpty()) {
        std::fprintf(stderr, "[repo_paths] REFUSED %s\n", resolution.refusal.toLocal8Bit().constData());
        std::fflush(stderr);
        std::fflush(stdout);
        std::exit(kRepoRootRefusedExitCode);
    }
    return resolution.root;
}

QString repo_file_path(const QString & relative_path)
{
    const QString root = find_repo_root();
    if (root.isEmpty()) {
        return QString();
    }
    return QDir(root).filePath(relative_path);
}
