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
 * unchanged() is false when the guard could not be taken or the stamp moved: then no source id is attached (fail closed).
 *
 * r8 (fable r7 key A1, adversary r7 A1 on 09973e83): the guard is taken at the start of the capture block, AFTER the measured
 * Play has decoded frames that the caches (raw prefetch ring, raw RGB cache, processed caches) later serve with no file read,
 * so a byte edited after such a decode and before the guard was baked into the guard's own stamp: the cached picture was the
 * old bytes, the deferred hash read the new ones. Now the guard must also MATCH the stamp taken when the clip was OPENED
 * (openStamp(), from the clip's own read handles, before any frame of it is decoded): any write to the files between the open
 * and the end of the hashing moves the stamp, the guard is not held, and no source id is attached. Every decode a cache can
 * serve happened after the open, so no cached picture can predate the bytes the identity is read from. */

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
    // r8: the stamp of the clip's files as they are NOW, read through the clip's own open handles (no reopen, no lock).
    // Taken when the clip is opened, before any frame of it is decoded. Empty when any file is missing or unreadable.
    static QByteArray openStamp( FILE * const *files, int count )
    {
        QByteArray out;
        if( !files || count <= 0 ) return out;
        for( int i = 0; i < count; ++i )
        {
            if( !files[i] ) return QByteArray();
#ifdef Q_OS_WIN
            const intptr_t os = _get_osfhandle( _fileno( files[i] ) );
            if( os == -1 || !appendStamp( out, reinterpret_cast<HANDLE>( os ) ) ) return QByteArray();
#else
            if( !appendStamp( out, fileno( files[i] ) ) ) return QByteArray();
#endif
        }
        return out;
    }

    // clipOpenStamp: openStamp() of these same files, taken when the clip was opened (r8). The guard is held only when the
    // files are still exactly what they were then; an empty clipOpenStamp (unknown open state) is never held.
    SourceWriteGuard( FILE * const *files, int count, const QByteArray &clipOpenStamp )
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
        // r8: a write since the clip was opened (before this guard) is a write the cached pictures may predate
        m_held = held && !m_stamp.isEmpty() && !clipOpenStamp.isEmpty() && m_stamp == clipOpenStamp;
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
    // One file's identity, size and last write time (POSIX: and change time), the same text for any handle of that file.
#ifdef Q_OS_WIN
    static bool appendStamp( QByteArray &out, HANDLE h )
    {
        BY_HANDLE_FILE_INFORMATION info;
        if( !GetFileInformationByHandle( h, &info ) ) return false;
        const quint64 values[] = {
            info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow, info.nFileSizeHigh, info.nFileSizeLow,
            info.ftLastWriteTime.dwHighDateTime, info.ftLastWriteTime.dwLowDateTime };
        for( quint64 value : values ) out += QByteArray::number( value ) + ':';
        out += ';';
        return true;
    }
#else
    static bool appendStamp( QByteArray &out, int fd )
    {
        struct stat st;
        if( fd < 0 || fstat( fd, &st ) != 0 ) return false;
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
        return true;
    }
#endif

    QByteArray stamp() const
    {
        QByteArray out;
#ifdef Q_OS_WIN
        for( HANDLE h : m_handles )
            if( !appendStamp( out, h ) ) return QByteArray();
#else
        for( int fd : m_handles )
            if( !appendStamp( out, fd ) ) return QByteArray();
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
