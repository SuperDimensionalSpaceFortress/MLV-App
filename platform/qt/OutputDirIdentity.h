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
//
// r5 (sol r4 BLOCKER): a file id is unique only inside one volume, and a cloned NTFS volume keeps its serial and
// every file index, so an id match alone refused two distinct directories. On Windows the id is now the
// FileIdInfo pair (64-bit serial + 128-bit id, unique on ReFS too; the 32-bit serial + 64-bit index is the
// fallback), and the final path is also read in the volume-GUID namespace. Local volumes are told apart by their
// mount-manager GUID, so when both directories resolve there the GUID path alone decides Same (a volume has no
// directory hard links, so a directory has one normalized path in it). Without both GUID paths (a network share,
// a volume outside the mount manager, POSIX) an id match still answers Same and fails closed.
// Known limit, POSIX: st_dev/st_ino are not unique on overlayfs without xino; that is a successor, not handled here.
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QString>
#include <QStringList>

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
    bool hasWideId = false;          // Windows FileIdInfo was read: the pair below replaces volume/index
    unsigned long long wideVolume = 0;
    unsigned long long wideIdLow = 0;
    unsigned long long wideIdHigh = 0;
    QString finalPath;   // resolved through every alias, no trailing separator
    QString identityPath;   // Windows: the final path as \\?\Volume{GUID}\... (else the NT form); empty on POSIX
    QString volumeName;     // Windows: the \\?\Volume{GUID} root of identityPath; empty unless the GUID form was read
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

// Pure: "\\?\Volume{GUID}" when the path is in the volume-GUID namespace, else empty.
inline QString volumeGuidRootOf(const QString &path)
{
    static const QString prefix = QStringLiteral("\\\\?\\Volume{");
    if (!path.startsWith(prefix, Qt::CaseInsensitive))
        return QString();
    const int close = path.indexOf(QLatin1Char('}'), prefix.size());
    if (close < 0 || (close + 1 < path.size() && path.at(close + 1) != QLatin1Char('\\')))
        return QString();
    return path.left(close + 1);
}

// Pure: the file ids are equal. The wide (FileIdInfo) pair is used only when both sides have it.
inline bool sameFileId(const DirIdentity &a, const DirIdentity &b)
{
    if (a.hasWideId && b.hasWideId)
        return a.wideVolume == b.wideVolume && a.wideIdLow == b.wideIdLow && a.wideIdHigh == b.wideIdHigh;
    return a.volume == b.volume && a.index == b.index;
}

// Pure: the relation of two identities that were both obtained (ok).
inline DirRelation relationOf(const DirIdentity &a, const DirIdentity &b, const PathRules &rules)
{
    const bool bothInGuidNamespace = !a.volumeName.isEmpty() && !b.volumeName.isEmpty();
    const bool samePath = a.finalPath.compare(b.finalPath, rules.pathCase) == 0
        || (!a.identityPath.isEmpty() && a.identityPath.compare(b.identityPath, rules.pathCase) == 0);
    // An id match decides Same only where the volume cannot be told apart (see the header comment).
    if (samePath || (!bothInGuidNamespace && sameFileId(a, b)))
        return DirRelation::Same;
    if (isInside(a.finalPath, b.finalPath, rules)
        || (bothInGuidNamespace && isInside(a.identityPath, b.identityPath, rules)))
        return DirRelation::FirstInsideSecond;
    if (isInside(b.finalPath, a.finalPath, rules)
        || (bothInGuidNamespace && isInside(b.identityPath, a.identityPath, rules)))
        return DirRelation::SecondInsideFirst;
    return DirRelation::Distinct;
}

#ifdef Q_OS_WIN
// FILE_ID_INFO and FileIdInfo (= 18) are declared only for _WIN32_WINNT >= 0x0602, and the app builds for 0x0601,
// so the documented layout is spelled here. Where the class is unsupported the call fails and the 64-bit index is used.
struct WideFileIdInfo
{
    ULONGLONG VolumeSerialNumber;
    BYTE FileId[16];
};
constexpr int kFileIdInfoClass = 18;

// The final path of an open handle in one namespace; false (with the win32 error) when it cannot be read.
inline bool finalPathOf(HANDLE handle, DWORD flags, QString *out, DWORD *error)
{
    std::vector<wchar_t> buffer(512);
    DWORD length = GetFinalPathNameByHandleW(handle, buffer.data(), static_cast<DWORD>(buffer.size()), flags);
    if (length >= buffer.size()) {
        buffer.resize(static_cast<size_t>(length) + 1);
        length = GetFinalPathNameByHandleW(handle, buffer.data(), static_cast<DWORD>(buffer.size()), flags);
    }
    *error = GetLastError();
    if (length == 0 || length >= buffer.size())
        return false;
    *out = withoutTrailingSeparators(QString::fromWCharArray(buffer.data(), static_cast<int>(length)),
                                     windowsPathRules());
    return true;
}

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
    WideFileIdInfo wide{};
    if (GetFileInformationByHandleEx(handle, static_cast<FILE_INFO_BY_HANDLE_CLASS>(kFileIdInfoClass), &wide,
                                     sizeof(wide))) {
        id.hasWideId = true;
        id.wideVolume = wide.VolumeSerialNumber;
        for (int i = 0; i < 8; ++i) {
            id.wideIdLow |= static_cast<unsigned long long>(wide.FileId[i]) << (8 * i);
            id.wideIdHigh |= static_cast<unsigned long long>(wide.FileId[8 + i]) << (8 * i);
        }
    }
    DWORD finalPathError = 0;
    if (!finalPathOf(handle, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS, &id.finalPath, &finalPathError)) {
        CloseHandle(handle);
        id.error = QStringLiteral("final_path_failed win32=%1").arg(finalPathError);
        return id;
    }
    // The GUID form names the volume uniquely; a network share or a volume outside the mount manager has none,
    // and then the NT form is kept and volumeName stays empty (an id match then still answers Same).
    if (finalPathOf(handle, FILE_NAME_NORMALIZED | VOLUME_NAME_GUID, &id.identityPath, &finalPathError))
        id.volumeName = volumeGuidRootOf(id.identityPath);
    else if (!finalPathOf(handle, FILE_NAME_NORMALIZED | VOLUME_NAME_NT, &id.identityPath, &finalPathError))
        id.identityPath.clear();
    CloseHandle(handle);
    id.volume = info.dwVolumeSerialNumber;
    id.index = (static_cast<unsigned long long>(info.nFileIndexHigh) << 32) | info.nFileIndexLow;
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
    if (!a.identityPath.isEmpty() || !b.identityPath.isEmpty())
        result.detail += QStringLiteral(" first_volume_path=%1 second_volume_path=%2").arg(a.identityPath, b.identityPath);
    result.relation = relationOf(a, b, rules);
    return result;
}

} // namespace detail

inline DirComparison compareDirectories(const QString &first, const QString &second)
{
    return detail::compareIdentities(detail::identityOf(first), detail::identityOf(second),
                                     detail::hostPathRules());
}

namespace detail {

// Creates each missing directory of `path`, outermost first, and appends every one it created to `created`.
inline bool createMissingDirectories(const QString &path, QStringList *created)
{
    QStringList missing;
    QString cursor = QDir::cleanPath(QFileInfo(path).absoluteFilePath());
    while (!QFileInfo::exists(cursor)) {
        missing.prepend(cursor);
        const QString parent = QFileInfo(cursor).absolutePath();
        if (parent == cursor)
            break;
        cursor = parent;
    }
    for (const QString &dir : missing) {
        if (!QDir().mkdir(dir))
            return false;
        created->append(dir);
    }
    return QFileInfo(path).isDir();
}

// Removes what createMissingDirectories made, innermost first; rmdir leaves a directory that is no longer empty.
inline void removeCreatedDirectories(const QStringList &created)
{
    for (auto it = created.crbegin(); it != created.crend(); ++it)
        QDir().rmdir(*it);
}

} // namespace detail

// r5 (opus key3 r4): identity needs both directories on disk, so this creates the missing ones, compares, and
// removes every directory it created unless the answer is Distinct. A refused run leaves no new directory.
inline DirComparison prepareSeparateDirectories(const QString &first, const QString &second)
{
    QStringList created;
    DirComparison result;
    if (!detail::createMissingDirectories(first, &created) || !detail::createMissingDirectories(second, &created))
        result.detail = QStringLiteral("reason=create_failed");
    else
        result = compareDirectories(first, second);
    if (result.relation != DirRelation::Distinct)
        detail::removeCreatedDirectories(created);
    return result;
}

} // namespace output_dir_identity

#endif
