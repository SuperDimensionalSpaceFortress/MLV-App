// CPU-SCOPE-LIVE-CRASH-FIX-1 adversary A1: mlvThreadExitCache with pthread_setspecific failing (ENOMEM). A failed registration
// must not hand back an object nothing will ever release. The macro is scoped to this file, and T has internal linkage, so the
// instantiation below is the only one that sees it.
#include <pthread.h>
#include <cerrno>

static int failingSetspecific(pthread_key_t, const void *)
{
    return ENOMEM;
}

#define pthread_setspecific failingSetspecific
#include "processing/ThreadExitCache.h"
#undef pthread_setspecific

namespace
{
struct InjectedT
{
    int value = 7;
};
}

int tlsExitOrderInjectedAccess()
{
    return mlvThreadExitCache<InjectedT>().value;
}
