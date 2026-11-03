#ifndef VISUALCAPTURESOURCEGUARD_H
#define VISUALCAPTURESOURCEGUARD_H

/* PLAYBACK-SOURCE-FRAME-IDENTITY-1 r7 (adversary r7 pass 1 A1 on 1a9748b6): the source ids are read AFTER the play
 * captures (Play must stop first), so the clip's bytes on disk could change in between under an unchanged clip token
 * (same object, same lifecycle generation): one edited byte of a captured frame lent the capture the id of a picture it
 * never showed, and a healthy static display read FROZEN_DISPLAY. The guard is taken when the capture run starts and held
 * until the hashing ends:
 *  - Windows: each chunk file is reopened (ReOpenFile) with share mode FILE_SHARE_READ only, so no writer can open it
 *    while the guard holds it, and the reopen itself fails when a writer already has it open (the guard is then not held);
 *  - every platform: a stamp of each file's identity, size and last write time, read from the guard's OWN handles (never
 *    from the clip object, which a FileOpen may have freed), must be what it was when the guard was taken.
 * unchanged() is false when the guard could not be taken or the stamp moved: then no source id is attached (fail closed). */

#include <QByteArray>
#include <QVector>

#include <cstdint>
#include <cstdio>

#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <io.h>
#else
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace visual_capture_source_difference
{

class SourceWriteGuard
{
public:
    SourceWriteGuard( FILE * const *files, int count )
    {
        bool held = files != nullptr && count > 0;
#ifdef Q_OS_WIN
        // looked up at run time: ReOpenFile is Vista+, and the toolchains' _WIN32_WINNT defaults differ
        typedef HANDLE ( WINAPI *ReOpenFileFn )( HANDLE, DWORD, DWORD, DWORD );
        const HMODULE kernel = GetModuleHandleW( L"kernel32.dll" );
        const ReOpenFileFn reopen = kernel
            ? reinterpret_cast<ReOpenFileFn>( reinterpret_cast<void *>( GetProcAddress( kernel, "ReOpenFile" ) ) ) : nullptr;
        if( !reopen ) held = false;
#endif
        for( int i = 0; held && i < count; ++i )
        {
            if( !files[i] ) { held = false; break; }
#ifdef Q_OS_WIN
            const intptr_t os = _get_osfhandle( _fileno( files[i] ) );
            const HANDLE reopened = os == -1 ? INVALID_HANDLE_VALUE
                : reopen( reinterpret_cast<HANDLE>( os ), GENERIC_READ, FILE_SHARE_READ, 0 );
            if( reopened == INVALID_HANDLE_VALUE || reopened == nullptr ) { held = false; break; }
            m_handles.append( reopened );
#else
            const int fd = dup( fileno( files[i] ) );
            if( fd < 0 ) { held = false; break; }
            m_handles.append( fd );
#endif
        }
        if( held ) m_stamp = stamp();
        m_held = held && !m_stamp.isEmpty();
    }

    ~SourceWriteGuard()
    {
#ifdef Q_OS_WIN
        for( HANDLE h : m_handles ) CloseHandle( h );
#else
        for( int fd : m_handles ) ::close( fd );
#endif
    }

    SourceWriteGuard( const SourceWriteGuard & ) = delete;
    SourceWriteGuard &operator=( const SourceWriteGuard & ) = delete;

    bool held() const { return m_held; }

    // the guard holds AND no file it covers was replaced, resized or written since it was taken
    bool unchanged() const { return m_held && stamp() == m_stamp; }

private:
    QByteArray stamp() const
    {
        QByteArray out;
#ifdef Q_OS_WIN
        for( HANDLE h : m_handles )
        {
            BY_HANDLE_FILE_INFORMATION info;
            if( !GetFileInformationByHandle( h, &info ) ) return QByteArray();
            const quint64 values[] = {
                info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow, info.nFileSizeHigh, info.nFileSizeLow,
                info.ftLastWriteTime.dwHighDateTime, info.ftLastWriteTime.dwLowDateTime };
            for( quint64 value : values ) out += QByteArray::number( value ) + ':';
            out += ';';
        }
#else
        for( int fd : m_handles )
        {
            struct stat st;
            if( fstat( fd, &st ) != 0 ) return QByteArray();
#if defined(Q_OS_MACOS) || defined(Q_OS_DARWIN)
            const long long mtimeNs = static_cast<long long>( st.st_mtimespec.tv_nsec );
            const long long ctimeNs = static_cast<long long>( st.st_ctimespec.tv_nsec );
#else
            const long long mtimeNs = static_cast<long long>( st.st_mtim.tv_nsec );
            const long long ctimeNs = static_cast<long long>( st.st_ctim.tv_nsec );
#endif
            const long long values[] = {
                static_cast<long long>( st.st_dev ), static_cast<long long>( st.st_ino ), static_cast<long long>( st.st_size ),
                static_cast<long long>( st.st_mtime ), mtimeNs, static_cast<long long>( st.st_ctime ), ctimeNs };
            for( long long value : values ) out += QByteArray::number( value ) + ':';
            out += ';';
        }
#endif
        return out;
    }

#ifdef Q_OS_WIN
    QVector<HANDLE> m_handles;
#else
    QVector<int> m_handles;
#endif
    QByteArray m_stamp;
    bool m_held = false;
};

} // namespace visual_capture_source_difference

#endif // VISUALCAPTURESOURCEGUARD_H
