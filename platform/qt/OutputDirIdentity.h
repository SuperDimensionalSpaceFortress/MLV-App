#ifndef OUTPUT_DIR_IDENTITY_H
#define OUTPUT_DIR_IDENTITY_H

// CONTACT-SHEET-SEEK-FRAME-LIST-APP-1 r3 (sol B1): do two output directories name the same place on disk?
// A path-string comparison misses junctions, symlinks, subst drives and 8.3 names, so this compares what the
// filesystem says. Both directories must already exist; anything that cannot be proved answers Unknown, and a
// caller treats only Distinct as safe.
#include <QDir>
#include <QFileInfo>
#include <QString>

#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <vector>
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
    unsigned long long volume = 0;
    unsigned long long index = 0;
    QString finalPath;   // resolved through every alias, no trailing separator
    QString error;
};

inline QString withoutTrailingSeparator(QString path)
{
    while (path.size() > 1 && (path.endsWith(QLatin1Char('\\')) || path.endsWith(QLatin1Char('/'))))
        path.chop(1);
    return path;
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
    id.finalPath = withoutTrailingSeparator(QString::fromWCharArray(buffer.data(), static_cast<int>(length)));
    id.ok = true;
    return id;
}
const QChar kSeparator = QLatin1Char('\\');
const Qt::CaseSensitivity kPathCase = Qt::CaseInsensitive;   // errs toward refusal on a case-sensitive folder
#else
inline DirIdentity identityOf(const QString &path)
{
    DirIdentity id;
    const QFileInfo info(path);
    if (!info.exists()) { id.error = QStringLiteral("does_not_exist"); return id; }
    if (!info.isDir()) { id.error = QStringLiteral("not_a_directory"); return id; }
    id.finalPath = withoutTrailingSeparator(info.canonicalFilePath());
    if (id.finalPath.isEmpty()) { id.error = QStringLiteral("canonical_path_failed"); return id; }
    id.ok = true;
    return id;
}
const QChar kSeparator = QLatin1Char('/');
const Qt::CaseSensitivity kPathCase = Qt::CaseSensitive;
#endif

inline bool isInside(const QString &inner, const QString &outer)
{
    const QString prefix = outer.endsWith(kSeparator) ? outer : outer + kSeparator;
    return inner.size() > prefix.size() && inner.startsWith(prefix, kPathCase);
}

} // namespace detail

inline DirComparison compareDirectories(const QString &first, const QString &second)
{
    DirComparison result;
    const detail::DirIdentity a = detail::identityOf(first);
    const detail::DirIdentity b = detail::identityOf(second);
    if (!a.ok || !b.ok) {
        result.detail = QStringLiteral("first=%1 second=%2")
            .arg(a.ok ? QStringLiteral("ok") : a.error, b.ok ? QStringLiteral("ok") : b.error);
        return result;
    }
    result.detail = QStringLiteral("first_final=%1 second_final=%2").arg(a.finalPath, b.finalPath);
#ifdef Q_OS_WIN
    const bool sameObject = a.volume == b.volume && a.index == b.index;
#else
    const bool sameObject = false;
#endif
    if (sameObject || a.finalPath.compare(b.finalPath, detail::kPathCase) == 0)
        result.relation = DirRelation::Same;
    else if (detail::isInside(a.finalPath, b.finalPath))
        result.relation = DirRelation::FirstInsideSecond;
    else if (detail::isInside(b.finalPath, a.finalPath))
        result.relation = DirRelation::SecondInsideFirst;
    else
        result.relation = DirRelation::Distinct;
    return result;
}

} // namespace output_dir_identity

#endif
