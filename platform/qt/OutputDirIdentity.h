#ifndef OUTPUT_DIR_IDENTITY_H
#define OUTPUT_DIR_IDENTITY_H

// CONTACT-SHEET-SEEK-FRAME-LIST-APP-1 r3 (sol B1): do two output directories name the same place on disk?
// A path-string comparison misses junctions, symlinks, subst drives and 8.3 names, so this compares what the
// filesystem says. Both directories must already exist; anything that cannot be proved answers Unknown, and a
// caller treats only Distinct as safe.
//
// r4: Same is decided by object identity on every platform (Windows: volume serial + file index; POSIX: st_dev +
// st_ino of the canonical path), so a bind mount or other alias of the same directory compares Same.
// Supported scope, known limit: nesting is decided from the resolved path strings. A directory that is reachable
// only through a POSIX bind mount of an ancestor of the other directory is not detected as nested (proving that
// would need the mount table, which this does not parse). Separators are stripped by the platform's own rule:
// on POSIX a backslash is an ordinary filename character and is kept.
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QString>

#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <vector>
#else
#include <cerrno>
#include <sys/stat.h>
#endif

namespace output_dir_identity {

enum class DirRelation { Distinct, Same, FirstInsideSecond, SecondInsideFirst, Unknown };

struct DirComparison
{
    DirRelation relation = DirRelation::Unknown;
    QString detail;      // why identity could not be obtained, or the resolved paths that decided it
};

namespace detail {

struct DirIdentity
{
    bool ok = false;
    unsigned long long volume = 0;   // Windows volume serial / POSIX st_dev
    unsigned long long index = 0;    // Windows file index / POSIX st_ino
    QString finalPath;   // resolved through every alias, no trailing separator
    QString error;
};

// The path rules of one platform, as data, so both rules are testable on any host.
struct PathRules
{
    QString separators;              // every character that separates path components
    QChar separator;                 // the one a resolved path uses
    Qt::CaseSensitivity pathCase;
    bool driveRoots;                 // "C:\" is a root and keeps its separator
};

inline PathRules windowsPathRules()
{
    // Case-insensitive errs toward refusal on a case-sensitive folder.
    return PathRules{ QStringLiteral("\\/"), QLatin1Char('\\'), Qt::CaseInsensitive, true };
}

inline PathRules posixPathRules()
{
    return PathRules{ QStringLiteral("/"), QLatin1Char('/'), Qt::CaseSensitive, false };
}

inline PathRules hostPathRules()
{
#ifdef Q_OS_WIN
    return windowsPathRules();
#else
    return posixPathRules();
#endif
}

// Strips trailing separators of the given rule only; a root ("/", "C:\", "\\?\C:\") is kept intact.
inline QString withoutTrailingSeparators(QString path, const PathRules &rules)
{
    while (path.size() > 1 && rules.separators.contains(path.back())) {
        if (rules.driveRoots && path.size() >= 2 && path.at(path.size() - 2) == QLatin1Char(':'))
            break;
        path.chop(1);
    }
    return path;
}

inline bool isInside(const QString &inner, const QString &outer, const PathRules &rules)
{
    const QString prefix = outer.endsWith(rules.separator) ? outer : outer + rules.separator;
    return inner.size() > prefix.size() && inner.startsWith(prefix, rules.pathCase);
}

// Pure: the relation of two identities that were both obtained (ok).
inline DirRelation relationOf(const DirIdentity &a, const DirIdentity &b, const PathRules &rules)
{
    const bool sameObject = a.volume == b.volume && a.index == b.index;
    if (sameObject || a.finalPath.compare(b.finalPath, rules.pathCase) == 0)
        return DirRelation::Same;
    if (isInside(a.finalPath, b.finalPath, rules))
        return DirRelation::FirstInsideSecond;
    if (isInside(b.finalPath, a.finalPath, rules))
        return DirRelation::SecondInsideFirst;
    return DirRelation::Distinct;
}

#ifdef Q_OS_WIN
inline DirIdentity identityOf(const QString &path)
{
    DirIdentity id;
    const std::wstring native = QDir::toNativeSeparators(QFileInfo(path).absoluteFilePath()).toStdWString();
    // FILE_FLAG_BACKUP_SEMANTICS opens a directory; without FILE_FLAG_OPEN_REPARSE_POINT a junction or
    // symlink is followed, so the handle is the target directory itself.
    const HANDLE handle = CreateFileW(native.c_str(), FILE_READ_ATTRIBUTES,
                                      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                                      OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, nullptr);
    if (handle == INVALID_HANDLE_VALUE) {
        id.error = QStringLiteral("open_failed win32=%1").arg(GetLastError());
        return id;
    }
    BY_HANDLE_FILE_INFORMATION info{};
    if (!GetFileInformationByHandle(handle, &info)) {
        id.error = QStringLiteral("file_information_failed win32=%1").arg(GetLastError());
        CloseHandle(handle);
        return id;
    }
    if (!(info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
        id.error = QStringLiteral("not_a_directory");
        CloseHandle(handle);
        return id;
    }
    std::vector<wchar_t> buffer(512);
    DWORD length = GetFinalPathNameByHandleW(handle, buffer.data(), static_cast<DWORD>(buffer.size()),
                                             FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (length >= buffer.size()) {
        buffer.resize(static_cast<size_t>(length) + 1);
        length = GetFinalPathNameByHandleW(handle, buffer.data(), static_cast<DWORD>(buffer.size()),
                                           FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    }
    const DWORD finalPathError = GetLastError();
    CloseHandle(handle);
    if (length == 0 || length >= buffer.size()) {
        id.error = QStringLiteral("final_path_failed win32=%1").arg(finalPathError);
        return id;
    }
    id.volume = info.dwVolumeSerialNumber;
    id.index = (static_cast<unsigned long long>(info.nFileIndexHigh) << 32) | info.nFileIndexLow;
    id.finalPath = withoutTrailingSeparators(QString::fromWCharArray(buffer.data(), static_cast<int>(length)),
                                             windowsPathRules());
    id.ok = true;
    return id;
}
#else
inline DirIdentity identityOf(const QString &path)
{
    DirIdentity id;
    const QFileInfo info(path);
    if (!info.exists()) { id.error = QStringLiteral("does_not_exist"); return id; }
    if (!info.isDir()) { id.error = QStringLiteral("not_a_directory"); return id; }
    const QString canonical = info.canonicalFilePath();
    if (canonical.isEmpty()) { id.error = QStringLiteral("canonical_path_failed"); return id; }
    // The object identity: a bind mount or any other alias of the same directory has the same st_dev/st_ino.
    struct stat st;
    if (::stat(QFile::encodeName(canonical).constData(), &st) != 0) {
        id.error = QStringLiteral("stat_failed errno=%1").arg(errno);
        return id;
    }
    if (!S_ISDIR(st.st_mode)) { id.error = QStringLiteral("not_a_directory"); return id; }
    id.volume = static_cast<unsigned long long>(st.st_dev);
    id.index = static_cast<unsigned long long>(st.st_ino);
    id.finalPath = withoutTrailingSeparators(canonical, posixPathRules());
    id.ok = true;
    return id;
}
#endif

// Pure: identities in, comparison out (so the pair can be injected by a test).
inline DirComparison compareIdentities(const DirIdentity &a, const DirIdentity &b, const PathRules &rules)
{
    DirComparison result;
    if (!a.ok || !b.ok) {
        result.detail = QStringLiteral("first=%1 second=%2")
            .arg(a.ok ? QStringLiteral("ok") : a.error, b.ok ? QStringLiteral("ok") : b.error);
        return result;
    }
    result.detail = QStringLiteral("first_final=%1 second_final=%2").arg(a.finalPath, b.finalPath);
    result.relation = relationOf(a, b, rules);
    return result;
}

} // namespace detail

inline DirComparison compareDirectories(const QString &first, const QString &second)
{
    return detail::compareIdentities(detail::identityOf(first), detail::identityOf(second),
                                     detail::hostPathRules());
}

} // namespace output_dir_identity

#endif
