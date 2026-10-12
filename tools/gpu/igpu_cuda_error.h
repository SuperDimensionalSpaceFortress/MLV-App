/* igpu_cuda_error.h - C ABI for the last CUDA error a backend DLL saw
 * (GPU-FIELD-TELEMETRY-1).
 *
 * Both CUDA backend DLLs keep ONE DLL-global slot holding the most recent
 * non-success cudaError_t any of their failure branches observed (CK / CKN /
 * check_cuda and the explicit map/copy/unmap rc checks). The slot is written
 * only on failure, under a mutex; the success path is unchanged. The app's
 * field telemetry reads it after a backend call returns rc != 0, so a sticky
 * fault (700 / 702 / 719) lands in the local log with the call site that saw it.
 *
 * Both functions are OPTIONAL exports: older DLLs omit them, and callers must
 * resolve them with GetProcAddress and treat a missing symbol as "no CUDA code".
 * Each returns 0 and copies the slot (code 0 = no error seen yet), or -1 for a
 * NULL out pointer.
 */
#ifndef IGPU_CUDA_ERROR_H
#define IGPU_CUDA_ERROR_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int code;                 /* cudaError_t value; 0 = none */
    char name[48];            /* cudaGetErrorName(code), NUL-terminated */
    char site[96];            /* failing call / site text, truncated, NUL-terminated */
    double utc_ms;            /* wall clock when noted, Unix epoch ms */
    unsigned long long count; /* failures noted since the DLL loaded */
} igpu_cuda_error_t;

int igpu_recon_last_cuda_error(igpu_cuda_error_t * out);
int igpu_amaze_debayer_last_cuda_error(igpu_cuda_error_t * out);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* IGPU_CUDA_ERROR_H */
