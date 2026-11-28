// DUALISO-CONFIGURE-CACHE-1: the alternate ISO pattern dll_test.cpp feeds igpu_recon_set_iso_pattern to prove the
// backend consumes a pattern change. Header-only and dependency-free so a unit test can enumerate every pattern.
//
// sol r6 B1: the alternate used to be the half swap alone. A truth pattern that is its own half swap
// (1,0,1,0 / 0,1,0,1 / 0,0,0,0 / 1,1,1,1) got itself back as the alternate, the output could not differ, and a
// correct backend was reported as failing. Now the half swap is used whenever it differs from the truth and the
// complement otherwise; the alternate differs from the truth for every input.
#pragma once

// 1 when the half swap of truth[4] differs from truth[4] (the half swap is then the alternate).
static inline int dll_test_alternate_is_half_swap(const int truth[4])
{
    return truth[2] != truth[0] || truth[3] != truth[1];
}

// Fills other[4] with the pattern to try against `truth`. Returns 1 when other differs from truth (always).
static inline int dll_test_pick_alternate_iso_pattern(const int truth[4], int other[4])
{
    if (dll_test_alternate_is_half_swap(truth)) {
        other[0] = truth[2];
        other[1] = truth[3];
        other[2] = truth[0];
        other[3] = truth[1];
    } else {
        other[0] = truth[0] ? 0 : 1;
        other[1] = truth[1] ? 0 : 1;
        other[2] = truth[2] ? 0 : 1;
        other[3] = truth[3] ? 0 : 1;
    }
    return other[0] != truth[0] || other[1] != truth[1] || other[2] != truth[2] || other[3] != truth[3];
}
