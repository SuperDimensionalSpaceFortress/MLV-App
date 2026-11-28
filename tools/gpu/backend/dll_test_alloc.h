// DUALISO-CONFIGURE-CACHE-1 r17 (sol 2): the DLL harness's checked buffer allocation. dll_test.cpp sizes its
// buffers from the vector's own dimensions (the res8k vector is 8192x4320, 70,778,880 bytes per uint16 plane), so a
// failed allocation is a reachable input, and a NULL passed on to memset or a run call is a crash instead of a
// diagnostic. Header-only and dependency-free so a unit test can inject an allocator that fails.
#pragma once

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

typedef void * (*dll_test_alloc_fn)(size_t bytes);

// Returns a zero-filled buffer of `count` uint16_t, or NULL after writing one typed line to stderr
// ("[dll_test] allocation failed: <what> (<bytes> bytes)"). The caller reports a non-zero exit; it never uses the
// result unchecked. A count whose byte size overflows size_t is refused the same way, without calling `alloc`.
static inline uint16_t * dll_test_alloc_u16_zeroed(size_t count, const char * what, dll_test_alloc_fn alloc)
{
    if (count == 0 || count > (size_t)-1 / sizeof(uint16_t)) {
        fprintf(stderr, "[dll_test] allocation failed: %s (%zu elements: zero, or a size that overflows)\n", what, count);
        return NULL;
    }
    const size_t bytes = count * sizeof(uint16_t);
    uint16_t * p = (uint16_t *)alloc(bytes);
    if (!p) {
        fprintf(stderr, "[dll_test] allocation failed: %s (%zu bytes)\n", what, bytes);
        return NULL;
    }
    memset(p, 0, bytes);
    return p;
}
