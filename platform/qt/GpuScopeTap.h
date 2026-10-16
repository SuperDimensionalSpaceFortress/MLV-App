/*!
 * \file GpuScopeTap.h
 * \author Claude
 * \copyright 2026
 * \brief PLAYBACK-GPU-SCOPES-NO-VETO-1: the Throttled scope's async read-back of a picture a GL surface drew.
 *
 * One state machine, owned by value by both GL presenters (GpuDisplayViewport, the QOpenGLWidget viewport,
 * and GpuDisplayWindow, the QOpenGLWindow preview). A request makes the surface's next paint blit the drawn
 * image rectangle into a small FBO and read it into one of two PBOs behind a fence; a later paint polls the
 * fence with a zero timeout and maps the buffer only once it has signalled -- never blocking, never glFinish.
 * take() hands the converted RGB8 picture to the scope and never calls GL.
 *
 * Not a QObject and not thread-safe: every call runs on the surface's GUI thread. GL is only ever called with
 * the context the names were created in current; destroy() without it only forgets the names
 * (CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 is open on teardown).
 */
#ifndef GPUSCOPETAP_H
#define GPUSCOPETAP_H

#include <QOpenGLContext>
#include <QOpenGLExtraFunctions>
#include <QSurfaceFormat>
#include <QtGlobal>

#include <cstdint>
#include <vector>

class GpuScopeTap
{
public:
    struct Counters
    {
        quint64 requests = 0;    // request() calls
        quint64 issued = 0;      // read-backs issued by a paint
        quint64 completed = 0;   // fences signalled, buffers mapped and converted
        quint64 notReady = 0;    // requests DROPPED because both read-backs were still in flight (CoS #447)
        quint64 skipped = 0;     // a requested read-back the paint did not issue (rect outside the framebuffer, no FBO)
        double mapMsTotal = 0.0; // map + convert + unmap, summed over completed
        int lastWidth = 0;
        int lastHeight = 0;
    };

    /* surfaceName is logged on the supported() line: "widget" or "window". */
    explicit GpuScopeTap(const char *surfaceName);
    GpuScopeTap(const GpuScopeTap &) = delete;
    GpuScopeTap &operator=(const GpuScopeTap &) = delete;

    /* Desktop GL (not ES) at >= 3.2, or GL_ARB_sync plus a framebuffer blit; QOpenGLExtraFunctions; and a
     * single-sample framebuffer, requested and realized (a scaled blit cannot resolve multisample). Cached per
     * context; logs one playback_scope_gpu_tap line per (re)evaluation. Evaluates only with context current
     * (extension queries need it), so the surfaces call it at the start of every paint -- a pointer compare once
     * cached -- and everything outside a paint reads supportedCached(). */
    bool supported(QOpenGLContext *context, const QSurfaceFormat &requested) const;
    /* The answer supported() cached for this context; false when it has not been evaluated for it yet. No GL. */
    bool supportedCached(const QOpenGLContext *context) const
    {
        return context && m_supportContext == context && m_supportCached;
    }

    /* Ask the next paint for a read-back at most maxWidth wide. Dropped (counted in notReady) only when both
     * read-backs are still in flight. */
    void request(int maxWidth);
    bool requested(void) const { return m_requested; }
    void clearRequest(void) { m_requested = false; }

    /* Right after the surface drew its picture into sourceFbo: the image rectangle (top-down framebuffer pixels,
     * edges as drawn) and the framebuffer size. context must be current and supported() true for it. Skips
     * (counted) otherwise, or when the rectangle is not wholly inside the framebuffer. */
    void issue(QOpenGLContext *context, GLuint sourceFbo,
               double leftPx, double topPx, double rightPx, double bottomPx, int fbWidth, int fbHeight);

    /* PLAYBACK-GPU-SCOPES-NO-VETO-1 D2 (CoS #447 blocker 5): every paint that drew a picture records whether its
     * image rectangle could be read back, so the scope is never handed to a read-back that would only skip. */
    void noteDrawnRect(double leftPx, double topPx, double rightPx, double bottomPx, int fbWidth, int fbHeight);
    bool drawnRectCapturable(void) const { return m_drawnRectCapturable; }

    /* Zero-timeout fence poll: maps a finished read-back, never waits for one. Only with context current. */
    void poll(QOpenGLContext *context);

    /* The newest finished read-back (RGB8, top-down rows), once. No GL. */
    bool take(std::vector<uint8_t> *rgb8, int *width, int *height, quint64 *serial);

    /* D3 (CoS #447 improvement): stop, seek, clip or mode change. Drops the pending request and the finished
     * result, and marks in-flight read-backs stale so poll() frees them without producing a result. No GL. */
    void invalidate(void);

    /* contextCurrent: the context these names were created in is current, so they are deleted. Otherwise they
     * are only forgotten. Either way the support cache is reset (a context re-created at the same address is
     * re-evaluated). */
    void destroy(bool contextCurrent);

    bool pending(void) const;
    const Counters &counters(void) const { return m_counters; }

private:
    friend class GuiSmokeTest; // drives the no-GL state (request/take/invalidate) without a context

    struct Slot
    {
        GLuint pbo = 0;
        GLsync fence = nullptr;
        int width = 0;
        int height = 0;
        int capacityBytes = 0;
        quint64 serial = 0;
        quint64 generation = 0;
    };

    const char *m_surfaceName;
    Slot m_slots[2];
    GLuint m_fbo = 0;
    GLuint m_color = 0;
    int m_fboWidth = 0;
    int m_fboHeight = 0;
    QOpenGLContext *m_namesContext = nullptr; // the context the FBO/PBO/fence names belong to
    bool m_requested = false;
    int m_maxWidth = 512;
    quint64 m_issueSerial = 0;
    quint64 m_generation = 0;
    bool m_drawnRectCapturable = false;
    std::vector<uint8_t> m_result;
    int m_resultWidth = 0;
    int m_resultHeight = 0;
    quint64 m_resultSerial = 0;
    bool m_resultReady = false;
    Counters m_counters;
    mutable const QOpenGLContext *m_supportContext = nullptr;
    mutable bool m_supportCached = false;
};

#endif // GPUSCOPETAP_H
