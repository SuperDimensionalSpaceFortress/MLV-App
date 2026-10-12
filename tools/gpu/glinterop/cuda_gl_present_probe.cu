// cuda_gl_present_probe.cu - Tier 2 ZERO-READBACK present probe for the RTX 4090.
//
// Proves that a *device-resident* recon-output buffer can be delivered into an
// OpenGL texture with NO CPU readback (no glReadPixels, no cudaMemcpyDeviceToHost),
// using CUDA<->GL interop, and times that "interop present" against the CPU
// readback (cudaMemcpyDeviceToHost) it replaces.
//
// Context: the Tier 1 GL path lost its seams to a per-frame device->host copy
// (~1.2 ms @ 4.1 MP measured by cuda_recon_probe). If the recon output can be
// blitted straight into a GL texture on-device, that D2H disappears entirely.
//
// Standalone, Windows, no GLFW/GLEW: raw WGL + opengl32 on a hidden Win32 window.
//
// Pipeline measured per "present":
//   cudaGraphicsMapResources
//   cudaGraphicsSubResourceGetMappedArray
//   cudaMemcpy2DToArray(device -> CUDA array backing the GL texture, D2D)
//   cudaGraphicsUnmapResources
// vs baseline:
//   cudaMemcpyDeviceToHost(same-size buffer)
//
// CAVEAT: a raw-WGL window in a non-interactive / Session-0 (SSH) context may not
// get a hardware GL context. If wglCreateContext / wglMakeCurrent fails, or
// GL_RENDERER reports Intel UHD / GDI / llvmpipe / software, that is the blocker
// and the probe says so explicitly (and still reports the D2H baseline, which
// needs no GL). The production app (MLVApp/Qt) already gets a 4090 GL context over
// SSH (it exports the same discrete-GPU request), so zero-readback present is viable in production even
// if this raw-WGL probe needs an interactive desktop session.
//
// Usage: cuda_gl_present_probe.exe [width] [height] [iters]
//   defaults: 1808 2268 (4.1 MP, the test-clip resolution), 60 iters.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <GL/gl.h>

#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>

#include <cuda_runtime.h>
#include <cuda_gl_interop.h>

// Ask the hybrid-GPU driver for the discrete GPU the same way MLVApp does (platform/qt/main.cpp), so no
// per-app Windows GPU preference has to be written (NA-8: that registry state is the owner's).
extern "C" {
__declspec(dllexport) unsigned long NvOptimusEnablement = 0x00000001;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}

// ---- GL enums / typedefs not guaranteed in the ancient <GL/gl.h> MS ships ----
#ifndef GL_BGRA
#define GL_BGRA 0x80E1
#endif
#ifndef GL_CLAMP_TO_EDGE
#define GL_CLAMP_TO_EDGE 0x812F
#endif

#define CK(call) do{ cudaError_t e=(call); if(e!=cudaSuccess){ \
    printf("[FAIL] CUDA err %s:%d: %s\n",__FILE__,__LINE__,cudaGetErrorString(e)); \
    fflush(stdout); exit(3);} }while(0)

static float timed(cudaEvent_t a, cudaEvent_t b){ float ms=0; cudaEventElapsedTime(&ms,a,b); return ms; }

// Minimal hidden-window WGL bootstrap. Returns false (and prints the blocker) if
// no GL context can be created at all.
struct GLCtx {
    HWND  hwnd = nullptr;
    HDC   hdc  = nullptr;
    HGLRC hglrc= nullptr;
    bool ok = false;
};

static LRESULT CALLBACK WndProc(HWND h, UINT m, WPARAM w, LPARAM l){ return DefWindowProc(h,m,w,l); }

static GLCtx createHiddenGL(){
    GLCtx c;
    WNDCLASSA wc; memset(&wc,0,sizeof(wc));
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = GetModuleHandle(nullptr);
    wc.lpszClassName = "MlvCudaGlProbeWnd";
    wc.style         = CS_OWNDC;
    if(!RegisterClassA(&wc)){
        printf("[FAIL] RegisterClassA failed (err=%lu)\n",(unsigned long)GetLastError());
        return c;
    }
    // WS_OVERLAPPED, never shown.
    c.hwnd = CreateWindowExA(0, wc.lpszClassName, "cuda-gl-probe",
                             WS_OVERLAPPED, 0,0, 64,64,
                             nullptr,nullptr, wc.hInstance, nullptr);
    if(!c.hwnd){ printf("[FAIL] CreateWindowExA failed (err=%lu) -- no window station? (Session 0/SSH)\n",
                        (unsigned long)GetLastError()); return c; }
    c.hdc = GetDC(c.hwnd);
    if(!c.hdc){ printf("[FAIL] GetDC failed (err=%lu)\n",(unsigned long)GetLastError()); return c; }

    PIXELFORMATDESCRIPTOR pfd; memset(&pfd,0,sizeof(pfd));
    pfd.nSize        = sizeof(pfd);
    pfd.nVersion     = 1;
    pfd.dwFlags      = PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER;
    pfd.iPixelType   = PFD_TYPE_RGBA;
    pfd.cColorBits   = 32;
    pfd.cDepthBits   = 24;
    pfd.iLayerType   = PFD_MAIN_PLANE;
    int pf = ChoosePixelFormat(c.hdc, &pfd);
    if(pf==0){ printf("[FAIL] ChoosePixelFormat returned 0 (err=%lu) -- no GL pixel format available\n",
                      (unsigned long)GetLastError()); return c; }
    if(!SetPixelFormat(c.hdc, pf, &pfd)){
        printf("[FAIL] SetPixelFormat failed (err=%lu)\n",(unsigned long)GetLastError()); return c; }

    // Report what we actually got (software/GDI formats lack PFD_GENERIC_* flags clear).
    PIXELFORMATDESCRIPTOR got; memset(&got,0,sizeof(got)); got.nSize=sizeof(got); got.nVersion=1;
    DescribePixelFormat(c.hdc, pf, sizeof(got), &got);
    bool generic = (got.dwFlags & PFD_GENERIC_FORMAT)!=0;
    bool accel   = (got.dwFlags & PFD_GENERIC_ACCELERATED)!=0;
    printf("pixelformat=%d  generic=%d generic_accelerated=%d colorbits=%d\n",
           pf, generic?1:0, accel?1:0, got.cColorBits);

    c.hglrc = wglCreateContext(c.hdc);
    if(!c.hglrc){ printf("[FAIL] wglCreateContext failed (err=%lu) -- no GL driver context (likely Session 0/SSH)\n",
                         (unsigned long)GetLastError()); return c; }
    if(!wglMakeCurrent(c.hdc, c.hglrc)){
        printf("[FAIL] wglMakeCurrent failed (err=%lu)\n",(unsigned long)GetLastError());
        wglDeleteContext(c.hglrc); c.hglrc=nullptr; return c; }
    c.ok = true;
    return c;
}

static void destroyGL(GLCtx& c){
    wglMakeCurrent(nullptr,nullptr);
    if(c.hglrc) wglDeleteContext(c.hglrc);
    if(c.hwnd && c.hdc) ReleaseDC(c.hwnd, c.hdc);
    if(c.hwnd) DestroyWindow(c.hwnd);
}

int main(int argc,char**argv){
    int w = argc>1?atoi(argv[1]):1808;
    int h = argc>2?atoi(argv[2]):2268;
    int iters = argc>3?atoi(argv[3]):60;
    size_t n  = (size_t)w*h;
    size_t bytes = n*4;   // RGBA8

    printf("=== CUDA<->GL zero-readback present probe ===\n");
    printf("%dx%d (%.1f MP) RGBA8 = %.2f MB/frame  iters=%d\n", w,h, n/1e6, bytes/1e6, iters);

    cudaDeviceProp p; CK(cudaGetDeviceProperties(&p,0));
    printf("cuda device=%s  sm=%d.%d\n", p.name, p.major, p.minor);
    fflush(stdout);

    // ---- device-resident "recon output" buffer (the thing we want to present) ----
    uint8_t* d_recon=nullptr;
    CK(cudaMalloc(&d_recon, bytes));
    CK(cudaMemset(d_recon, 0x7F, bytes));   // arbitrary device-side contents

    // ===================================================================
    // BASELINE (always measurable, no GL): cudaMemcpyDeviceToHost.
    // This is the per-frame readback that zero-readback present eliminates.
    // ===================================================================
    uint8_t* h_pinned=nullptr;
    CK(cudaHostAlloc((void**)&h_pinned, bytes, cudaHostAllocDefault)); // pinned = best-case D2H
    uint8_t* h_paged = (uint8_t*)malloc(bytes);                        // pageable = typical glReadPixels dest

    cudaEvent_t a,b; CK(cudaEventCreate(&a)); CK(cudaEventCreate(&b));
    double accD2Hpinned=0, accD2Hpaged=0;
    int warm = iters-10; if(warm<1) warm=1;
    for(int it=0; it<iters; ++it){
        CK(cudaEventRecord(a));
        CK(cudaMemcpy(h_pinned, d_recon, bytes, cudaMemcpyDeviceToHost));
        CK(cudaEventRecord(b)); CK(cudaEventSynchronize(b));
        if(it>=10) accD2Hpinned += timed(a,b);

        CK(cudaEventRecord(a));
        CK(cudaMemcpy(h_paged, d_recon, bytes, cudaMemcpyDeviceToHost));
        CK(cudaEventRecord(b)); CK(cudaEventSynchronize(b));
        if(it>=10) accD2Hpaged += timed(a,b);
    }
    double d2hPinned = accD2Hpinned/warm;
    double d2hPaged  = accD2Hpaged/warm;
    printf("\n--- BASELINE: cudaMemcpyDeviceToHost (the readback being eliminated) ---\n");
    printf("  D2H pinned host  : %7.3f ms  (best-case readback)\n", d2hPinned);
    printf("  D2H pageable host: %7.3f ms  (typical glReadPixels-style dest)\n", d2hPaged);
    fflush(stdout);

    // ===================================================================
    // GL context + interop present.
    // ===================================================================
    GLCtx gl = createHiddenGL();
    if(!gl.ok){
        printf("\n[BLOCKER] Could not create a hardware WGL/GL context in this session.\n");
        printf("[BLOCKER] This is expected for a raw-WGL window under SSH / Session 0 (no window station/desktop).\n");
        printf("[BLOCKER] D2H baseline above is still valid. Zero-readback present is unmeasured here;\n");
        printf("[BLOCKER] run from an interactive desktop session to measure interop present.\n");
        printf("VERDICT: ZERO-READBACK present UNPROVEN in this session (GL context unavailable).\n");
        fflush(stdout);
        return 2;
    }

    const char* vendor   = (const char*)glGetString(GL_VENDOR);
    const char* renderer = (const char*)glGetString(GL_RENDERER);
    const char* version  = (const char*)glGetString(GL_VERSION);
    printf("\n--- GL context ---\n");
    printf("  GL_VENDOR  : %s\n", vendor?vendor:"(null)");
    printf("  GL_RENDERER: %s\n", renderer?renderer:"(null)");
    printf("  GL_VERSION : %s\n", version?version:"(null)");
    fflush(stdout);

    // Detect software / Intel renderer -> the interop-to-4090 story fails.
    auto contains=[&](const char* s, const char* sub)->bool{
        if(!s) return false;
        size_t ls=strlen(s), lp=strlen(sub);
        for(size_t i=0;i+lp<=ls;i++){ size_t j=0; for(;j<lp;j++){ char ca=s[i+j],cb=sub[j];
            if(ca>='A'&&ca<='Z')ca+=32; if(cb>='A'&&cb<='Z')cb+=32; if(ca!=cb)break;} if(j==lp)return true;}
        return false;
    };
    bool softwareGL = contains(renderer,"gdi") || contains(renderer,"llvmpipe") ||
                      contains(renderer,"software") || contains(renderer,"microsoft");
    bool intelGL    = contains(renderer,"intel");
    bool is4090     = contains(renderer,"4090") ||
                      (contains(renderer,"nvidia") && contains(renderer,"geforce"));

    if(softwareGL){
        printf("\n[BLOCKER] GL_RENDERER is a SOFTWARE rasterizer -- no GPU interop possible.\n");
        printf("VERDICT: ZERO-READBACK present UNPROVEN (software GL).\n");
        fflush(stdout); destroyGL(gl); return 2;
    }
    if(intelGL && !is4090){
        printf("\n[BLOCKER] GL_RENDERER is the Intel iGPU, NOT the RTX 4090.\n");
        printf("[BLOCKER] CUDA (device=%s) and GL would live on different GPUs -> interop register will fail or be slow.\n", p.name);
        printf("[BLOCKER] This probe already exports NvOptimusEnablement/AmdPowerXpressRequestHighPerformance; the session or driver still chose Intel. Run it on the interactive desktop session, or check the driver's hybrid-GPU setting.\n");
        printf("VERDICT: ZERO-READBACK present UNPROVEN (GL bound to Intel iGPU, not 4090).\n");
        fflush(stdout); destroyGL(gl); return 2;
    }
    if(!is4090){
        printf("\n[WARN] GL_RENDERER is not clearly the RTX 4090. Interop may still work if it is an NVIDIA GPU;\n");
        printf("[WARN] continuing, but treat the present numbers with care.\n");
    } else {
        printf("  -> GL is running on the RTX 4090. Good: CUDA and GL on the same GPU.\n");
    }
    fflush(stdout);

    // ---- GL texture (RGBA8) ----
    GLuint tex=0;
    glGenTextures(1,&tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    GLenum gerr = glGetError();
    if(gerr!=GL_NO_ERROR){ printf("[FAIL] glTexImage2D GL error 0x%04x\n", gerr); destroyGL(gl); return 4; }

    // ---- register the GL texture with CUDA (write-discard: we overwrite every frame) ----
    cudaGraphicsResource* res=nullptr;
    cudaError_t reg = cudaGraphicsGLRegisterImage(&res, tex, GL_TEXTURE_2D,
                                                  cudaGraphicsRegisterFlagsWriteDiscard);
    if(reg!=cudaSuccess){
        printf("\n[BLOCKER] cudaGraphicsGLRegisterImage failed: %s\n", cudaGetErrorString(reg));
        printf("[BLOCKER] This usually means the GL context is NOT on the CUDA device (wrong GPU)\n");
        printf("[BLOCKER] or the driver does not allow interop in this session.\n");
        printf("VERDICT: ZERO-READBACK present UNPROVEN (GL/CUDA interop registration failed).\n");
        fflush(stdout); destroyGL(gl); return 2;
    }
    printf("  cudaGraphicsGLRegisterImage: OK (WriteDiscard)\n");
    fflush(stdout);

    // ===================================================================
    // INTEROP PRESENT loop, cudaEvent-timed. NO glReadPixels, NO D2H.
    //   map -> getMappedArray -> cudaMemcpy2DToArray (D2D) -> unmap
    // ===================================================================
    double accPresent=0, accMap=0, accCopy=0, accUnmap=0;
    cudaEvent_t e0,e1,e2,e3; CK(cudaEventCreate(&e0)); CK(cudaEventCreate(&e1));
    CK(cudaEventCreate(&e2)); CK(cudaEventCreate(&e3));
    size_t rowBytes = (size_t)w*4;

    for(int it=0; it<iters; ++it){
        CK(cudaEventRecord(e0));
        CK(cudaGraphicsMapResources(1, &res, 0));
        CK(cudaEventRecord(e1));
        cudaArray_t arr=nullptr;
        CK(cudaGraphicsSubResourceGetMappedArray(&arr, res, 0, 0));
        // device -> CUDA array backing the GL texture. Pure on-GPU copy, no host touch.
        CK(cudaMemcpy2DToArray(arr, 0, 0, d_recon, rowBytes, rowBytes, (size_t)h,
                               cudaMemcpyDeviceToDevice));
        CK(cudaEventRecord(e2));
        CK(cudaGraphicsUnmapResources(1, &res, 0));
        CK(cudaEventRecord(e3));
        CK(cudaEventSynchronize(e3));
        if(it>=10){
            accMap     += timed(e0,e1);
            accCopy    += timed(e1,e2);
            accUnmap   += timed(e2,e3);
            accPresent += timed(e0,e3);
        }
    }
    double present = accPresent/warm;
    printf("\n--- INTEROP PRESENT (zero-readback): map -> D2D copy -> unmap ---\n");
    printf("  map resources     : %7.3f ms\n", accMap/warm);
    printf("  D2D copy->GL array : %7.3f ms\n", accCopy/warm);
    printf("  unmap resources   : %7.3f ms\n", accUnmap/warm);
    printf("  INTEROP PRESENT   : %7.3f ms  (NO glReadPixels, NO cudaMemcpyDeviceToHost)\n", present);

    // ---- comparison / verdict ----
    printf("\n--- COMPARISON ---\n");
    printf("  D2H pinned (eliminated)  : %7.3f ms\n", d2hPinned);
    printf("  D2H pageable (eliminated): %7.3f ms\n", d2hPaged);
    printf("  interop present (kept)   : %7.3f ms\n", present);
    if(present>0){
        printf("  speedup vs D2H pinned    : %.2fx\n", d2hPinned/present);
        printf("  speedup vs D2H pageable  : %.2fx\n", d2hPaged/present);
    }
    printf("  per-frame readback SAVED : %.3f ms (pinned) / %.3f ms (pageable)\n",
           d2hPinned-present, d2hPaged-present);
    printf("\nVERDICT: ZERO-READBACK present PROVEN on %s. Present costs %.3f ms vs %.3f ms D2H it replaces.\n",
           renderer?renderer:"(GL device)", present, d2hPinned);
    fflush(stdout);

    // cleanup
    CK(cudaGraphicsUnregisterResource(res));
    glDeleteTextures(1,&tex);
    destroyGL(gl);
    cudaFree(d_recon);
    cudaFreeHost(h_pinned);
    free(h_paged);
    return 0;
}
