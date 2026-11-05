#ifndef THREADEXITCACHE_H
#define THREADEXITCACHE_H

/* A per-thread cache object whose destructor runs at thread exit, for types with a non-trivial destructor.
 * T's destructor must read no thread-local: on MinGW it runs from a pthread key destructor, possibly after emutls has
 * already freed this thread's thread-local storage. tools/repo_hygiene/test_no_nontrivial_thread_local.py keeps new
 * thread_local objects from coming back. */

#if defined(__MINGW32__)
#include <pthread.h>
#include <cstdlib>
// Why: on mingw-w64 v11 the exe's TLS callback runs thread_local destructors after
// libgcc's emutls key destructor has freed their storage (CPU-SCOPE-LIVE-CRASH-FIX-1).
template <class T>
T & mlvThreadExitCache()
{
    static const pthread_key_t key = [] {
        pthread_key_t k;
        if (pthread_key_create(&k, [](void * p) { delete static_cast<T *>(p); }) != 0) std::abort();
        return k;
    }();
    void * p = pthread_getspecific(key);
    if (!p) { p = new T(); pthread_setspecific(key, p); }
    return *static_cast<T *>(p);
}
#else
template <class T>
T & mlvThreadExitCache() { static thread_local T t; return t; }
#endif

#endif // THREADEXITCACHE_H
