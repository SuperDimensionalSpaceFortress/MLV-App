/*!
 * \file GpuScopeTap.cpp
 * \author Claude
 * \copyright 2026
 * \brief PLAYBACK-GPU-SCOPES-NO-VETO-1: the shared scope read-back state machine (see header).
 */

#include "GpuScopeTap.h"
#include "PlaybackScopesDuringPlayback.h"

#include <QElapsedTimer>
#include <QRect>
#include <QSize>
#include <QtDebug>

#include <atomic>

namespace {

// r4: the production side of the GpuScopeTapGl seam -- every call forwards to the current context's functions.
class QtGpuScopeTapGl final : public GpuScopeTapGl
{
public:
    explicit QtGpuScopeTapGl(QOpenGLExtraFunctions *f) : m_f(f) {}
    GLenum glGetError() override { return m_f->glGetError(); }
    void glGenRenderbuffers(GLsizei n, GLuint *names) override { m_f->glGenRenderbuffers(n, names); }
    void glDeleteRenderbuffers(GLsizei n, const GLuint *names) override { m_f->glDeleteRenderbuffers(n, names); }
    void glBindRenderbuffer(GLenum target, GLuint name) override { m_f->glBindRenderbuffer(target, name); }
    void glRenderbufferStorage(GLenum target, GLenum format, GLsizei width, GLsizei height) override
    {
        m_f->glRenderbufferStorage(target, format, width, height);
    }
    void glGenFramebuffers(GLsizei n, GLuint *names) override { m_f->glGenFramebuffers(n, names); }
    void glDeleteFramebuffers(GLsizei n, const GLuint *names) override { m_f->glDeleteFramebuffers(n, names); }
    void glBindFramebuffer(GLenum target, GLuint name) override { m_f->glBindFramebuffer(target, name); }
    void glFramebufferRenderbuffer(GLenum target, GLenum attachment, GLenum rbTarget, GLuint rb) override
    {
        m_f->glFramebufferRenderbuffer(target, attachment, rbTarget, rb);
    }
    GLenum glCheckFramebufferStatus(GLenum target) override { return m_f->glCheckFramebufferStatus(target); }
    void glBlitFramebuffer(GLint sx0, GLint sy0, GLint sx1, GLint sy1,
                           GLint dx0, GLint dy0, GLint dx1, GLint dy1, GLbitfield mask, GLenum filter) override
    {
        m_f->glBlitFramebuffer(sx0, sy0, sx1, sy1, dx0, dy0, dx1, dy1, mask, filter);
    }
    void glGenBuffers(GLsizei n, GLuint *names) override { m_f->glGenBuffers(n, names); }
    void glBindBuffer(GLenum target, GLuint name) override { m_f->glBindBuffer(target, name); }
    void glBufferData(GLenum target, GLsizeiptr size, const void *data, GLenum usage) override
    {
        m_f->glBufferData(target, size, data, usage);
    }
    void glPixelStorei(GLenum pname, GLint param) override { m_f->glPixelStorei(pname, param); }
    void glReadPixels(GLint x, GLint y, GLsizei width, GLsizei height, GLenum format, GLenum type, void *pixels) override
    {
        m_f->glReadPixels(x, y, width, height, format, type, pixels);
    }
    GLsync glFenceSync(GLenum condition, GLbitfield flags) override { return m_f->glFenceSync(condition, flags); }
    GLenum glClientWaitSync(GLsync sync, GLbitfield flags, GLuint64 timeout) override
    {
        return m_f->glClientWaitSync(sync, flags, timeout);
    }
    void glDeleteSync(GLsync sync) override { m_f->glDeleteSync(sync); }
    void *glMapBufferRange(GLenum target, GLintptr offset, GLsizeiptr length, GLbitfield access) override
    {
        return m_f->glMapBufferRange(target, offset, length, access);
    }
    GLboolean glUnmapBuffer(GLenum target) override { return m_f->glUnmapBuffer(target); }

private:
    QOpenGLExtraFunctions *m_f;
};

// r6 (Dot I1): a presenter id is never 0 and never reused in the process, so a presenter re-created at the old one's
// address still reads as another presenter.
quint64 nextScopeTapPresenterId()
{
    static std::atomic<quint64> next{0};
    return next.fetch_add(1, std::memory_order_relaxed) + 1;
}

} // namespace

GpuScopeTap::GpuScopeTap(const char *surfaceName)
    : m_surfaceName(surfaceName ? surfaceName : "unknown")
{
    m_counters.presenterId = nextScopeTapPresenterId();
}

GpuScopeTap::Interval GpuScopeTap::interval(const Counters &start, const Counters &finish)
{
    Interval out;
    if ( start.presenterId != finish.presenterId ) return out;
    if ( finish.requests < start.requests || finish.completed < start.completed || finish.notReady < start.notReady
      || finish.skipped < start.skipped || finish.mapMsTotal < start.mapMsTotal )
        return out;
    out.valid = true;
    out.requests = finish.requests - start.requests;
    out.completed = finish.completed - start.completed;
    out.notReady = finish.notReady - start.notReady;
    out.skipped = finish.skipped - start.skipped;
    out.mapMsTotal = finish.mapMsTotal - start.mapMsTotal;
    return out;
}

bool GpuScopeTap::supported(QOpenGLContext *context, const QSurfaceFormat &requested) const
{
    if ( !context || !context->isValid() ) return false;
    if ( m_supportContext == context ) return m_supportCached;
    if ( QOpenGLContext::currentContext() != context ) return false; // not cached: re-asked from the next paint

    const QSurfaceFormat realized = context->format();
    bool isSupported = !context->isOpenGLES()
        && requested.samples() <= 0
        && realized.samples() <= 0
        && context->extraFunctions() != nullptr;
    if ( isSupported )
    {
        const bool core32 = realized.version() >= qMakePair(3, 2);
        const bool extensions = context->hasExtension(QByteArrayLiteral("GL_ARB_sync"))
            && ( context->hasExtension(QByteArrayLiteral("GL_ARB_framebuffer_object"))
                 || context->hasExtension(QByteArrayLiteral("GL_EXT_framebuffer_blit")) );
        isSupported = core32 || extensions;
    }
    m_supportContext = context;
    m_supportCached = isSupported;
    qInfo().nospace() << "playback_scope_gpu_tap supported=" << (isSupported ? 1 : 0)
                      << " gl=" << realized.majorVersion() << '.' << realized.minorVersion()
                      << " es=" << (context->isOpenGLES() ? 1 : 0)
                      << " samples=" << realized.samples()
                      << " surface=" << m_surfaceName;
    return isSupported;
}

bool GpuScopeTap::pending() const
{
    return m_slots[0].fence != nullptr || m_slots[1].fence != nullptr;
}

void GpuScopeTap::request(int maxWidth)
{
    ++m_counters.requests;
    // Both read-backs still in flight: drop this request rather than queue a third. Only a dropped request
    // counts as not ready (CoS #447: a request a free slot accepts is not "not ready").
    if ( m_slots[0].fence && m_slots[1].fence )
    {
        ++m_counters.notReady;
        return;
    }
    m_requested = true;
    m_maxWidth = qMax(1, maxWidth);
}

void GpuScopeTap::noteDrawnRect(double leftPx, double topPx, double rightPx, double bottomPx, int fbWidth, int fbHeight)
{
    m_drawnRectCapturable = playbackScopeTapFramebufferRect(leftPx, topPx, rightPx, bottomPx, fbWidth, fbHeight, nullptr);
}

void GpuScopeTap::issue(QOpenGLContext *context, GLuint sourceFbo,
                        double leftPx, double topPx, double rightPx, double bottomPx, int fbWidth, int fbHeight)
{
    QOpenGLExtraFunctions *functions =
        ( context && QOpenGLContext::currentContext() == context ) ? context->extraFunctions() : nullptr;
    QRect source;
    if ( !functions
         || !supportedCached(context)
         || ( m_namesContext && m_namesContext != context )
         || !playbackScopeTapFramebufferRect(leftPx, topPx, rightPx, bottomPx, fbWidth, fbHeight, &source) )
    {
        ++m_counters.skipped;
        return;
    }
    // Both slots busy means names already exist, and they belong to this context (checked above).
    m_namesContext = context;
    QtGpuScopeTapGl gl(functions);
    issueWith(&gl, sourceFbo, source);
}

// r4 (adversary r3 A2): a tap that only ever skips must not keep the scope off the CPU feed. A refused FBO is
// structural (it fails again every request), so it disables the tap at once; any other GL failure (a refused PBO, an
// errored blit/read, no fence, a failed map) does after kGlFailureLimit in a row. A published read-back resets the run.
void GpuScopeTap::noteGlFailure(const char *reason, bool structural)
{
    ++m_consecutiveGlFailures;
    if ( m_unusable ) return;
    if ( structural || m_consecutiveGlFailures >= kGlFailureLimit )
    {
        m_unusable = true;
        qWarning().nospace() << "playback_scope_gpu_tap unusable=1 reason=" << reason
                             << " consecutive_gl_failures=" << m_consecutiveGlFailures
                             << " gl_errors=" << m_counters.glErrors
                             << " surface=" << m_surfaceName << " fallback=cpu_scope_feed";
    }
}

void GpuScopeTap::issueWith(GpuScopeTapGl *gl, GLuint sourceFbo, const QRect &source)
{
    int slotIndex = -1;
    for ( int i = 0; i < 2 && slotIndex < 0; ++i )
    {
        const int candidate = static_cast<int>( ( m_issueSerial + static_cast<quint64>(i) ) % 2u );
        if ( !m_slots[candidate].fence ) slotIndex = candidate;
    }
    if ( slotIndex < 0 )
    {
        ++m_counters.skipped;
        return;
    }
    const QSize tapSize = playbackScopeTapSize(source.width(), source.height(), m_maxWidth);

    // r4 (adversary r3 A1 on b99325e7): every GL call from here to the fence is error-checked, and a read-back whose
    // sequence raised an error is discarded -- no fence, no serial, never published. A failed blit or glReadPixels can
    // leave the reused PBO holding the PREVIOUS picture, which would otherwise map cleanly and publish as this one.
    // Errors left pending by other code are drained first (bounded), so they are not blamed on this read.
    // r5 (adversary r4 A1 on 1d3ea3f5): drained BEFORE the tap's own FBO setup, not after it, so a setup error is never
    // drained away -- it is this request's own failure.
    for ( int drained = 0; drained < 8 && gl->glGetError() != GL_NO_ERROR; ++drained ) {}

    if ( !m_fbo || m_fboWidth != tapSize.width() || m_fboHeight != tapSize.height() )
    {
        if ( m_fbo ) gl->glDeleteFramebuffers(1, &m_fbo);
        if ( m_color ) gl->glDeleteRenderbuffers(1, &m_color);
        m_fbo = 0;
        m_color = 0;
        gl->glGenRenderbuffers(1, &m_color);
        gl->glBindRenderbuffer(GL_RENDERBUFFER, m_color);
        gl->glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA8, tapSize.width(), tapSize.height());
        gl->glBindRenderbuffer(GL_RENDERBUFFER, 0);
        gl->glGenFramebuffers(1, &m_fbo);
        gl->glBindFramebuffer(GL_FRAMEBUFFER, m_fbo);
        gl->glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, m_color);
        const GLenum status = gl->glCheckFramebufferStatus(GL_FRAMEBUFFER);
        // r5 (adversary r4 A1 on 1d3ea3f5): a setup call that raised an error, or a name that was never generated, is
        // a failed setup even when the status reads complete. glGenFramebuffers that fails leaves the name 0, and the
        // status of framebuffer 0 is the DISPLAYED framebuffer's (complete), so the blit would draw into the picture.
        const GLenum setupError = gl->glGetError();
        gl->glBindFramebuffer(GL_FRAMEBUFFER, sourceFbo);
        const bool incomplete = status != GL_FRAMEBUFFER_COMPLETE;
        if ( incomplete || setupError != GL_NO_ERROR || !m_fbo || !m_color )
        {
            if ( m_fbo ) gl->glDeleteFramebuffers(1, &m_fbo);
            if ( m_color ) gl->glDeleteRenderbuffers(1, &m_color);
            m_fbo = 0;
            m_color = 0;
            m_fboWidth = 0;
            m_fboHeight = 0;
            ++m_counters.skipped;
            // r5b (adversary r5 A1 on c8393c8f): a raised setup error is counted whichever branch disables the tap.
            if ( setupError != GL_NO_ERROR ) ++m_counters.glErrors;
            if ( incomplete ) noteGlFailure("fbo_incomplete", true);
            else noteGlFailure("fbo_setup", false);
            return;
        }
        m_fboWidth = tapSize.width();
        m_fboHeight = tapSize.height();
    }

    Slot &slot = m_slots[slotIndex];
    const int bytes = tapSize.width() * tapSize.height() * 4;
    if ( !slot.pbo ) gl->glGenBuffers(1, &slot.pbo);

    // r4: one check below covers the PBO name, the binds and the blit (the r4 drain now runs before the FBO setup).
    gl->glBindFramebuffer(GL_READ_FRAMEBUFFER, sourceFbo);
    gl->glBindFramebuffer(GL_DRAW_FRAMEBUFFER, m_fbo);
    gl->glBlitFramebuffer(source.left(), source.top(), source.left() + source.width(), source.top() + source.height(),
                          0, 0, tapSize.width(), tapSize.height(), GL_COLOR_BUFFER_BIT, GL_LINEAR);
    gl->glBindFramebuffer(GL_READ_FRAMEBUFFER, m_fbo);
    gl->glBindBuffer(GL_PIXEL_PACK_BUFFER, slot.pbo);
    // r5 (A1 sibling): no PBO name means glReadPixels would write to client address 0, so it fails like an error.
    if ( gl->glGetError() != GL_NO_ERROR || !slot.pbo )
    {
        gl->glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
        gl->glBindFramebuffer(GL_FRAMEBUFFER, sourceFbo);
        ++m_counters.skipped;
        ++m_counters.glErrors;
        noteGlFailure("blit", false);
        return;
    }
    if ( slot.capacityBytes < bytes )
    {
        // r5 (adversary r4 A2 on 6b564afc): the capacity is recorded only once the store exists. A refused allocation
        // (GL_OUT_OF_MEMORY) leaves an empty store; caching its size would skip every later allocation and never complete
        // a read-back. No error is pending here (checked just above), so the next one is this allocation's.
        gl->glBufferData(GL_PIXEL_PACK_BUFFER, bytes, nullptr, GL_STREAM_READ);
        if ( gl->glGetError() != GL_NO_ERROR )
        {
            slot.capacityBytes = 0;   // nothing allocated: the next request allocates again
            gl->glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
            gl->glBindFramebuffer(GL_FRAMEBUFFER, sourceFbo);
            ++m_counters.skipped;
            ++m_counters.glErrors;
            noteGlFailure("pbo_allocate", false);
            return;
        }
        slot.capacityBytes = bytes;
    }
    gl->glPixelStorei(GL_PACK_ALIGNMENT, 4);
    // Into the bound PBO (offset 0): the GPU copies asynchronously; nothing reaches client memory here.
    gl->glReadPixels(0, 0, tapSize.width(), tapSize.height(), GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    const GLenum readError = gl->glGetError();
    gl->glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
    if ( readError != GL_NO_ERROR )
    {
        gl->glBindFramebuffer(GL_FRAMEBUFFER, sourceFbo);
        ++m_counters.skipped;
        ++m_counters.glErrors;
        noteGlFailure("read_pixels", false);
        return;
    }
    slot.fence = gl->glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
    gl->glBindFramebuffer(GL_FRAMEBUFFER, sourceFbo);
    if ( !slot.fence )
    {
        ++m_counters.skipped;
        ++m_counters.glErrors;   // r5b (adversary r5 A1 sibling :286): glFenceSync returns null only on a GL error
        noteGlFailure("fence", false);
        return;
    }
    slot.width = tapSize.width();
    slot.height = tapSize.height();
    slot.serial = ++m_issueSerial;
    slot.generation = m_generation;
    ++m_counters.issued;
}

void GpuScopeTap::poll(QOpenGLContext *context)
{
    if ( !pending() ) return;
    if ( !context || QOpenGLContext::currentContext() != context || m_namesContext != context ) return;
    QOpenGLExtraFunctions *functions = context->extraFunctions();
    if ( !functions ) return;
    QtGpuScopeTapGl gl(functions);
    pollWith(&gl);
}

void GpuScopeTap::pollWith(GpuScopeTapGl *gl)
{
    // Oldest first, so a newer read-back is never overwritten by an older one.
    int order[2] = { 0, 1 };
    if ( m_slots[1].fence && ( !m_slots[0].fence || m_slots[1].serial < m_slots[0].serial ) )
    {
        order[0] = 1;
        order[1] = 0;
    }
    for ( int i : order )
    {
        Slot &slot = m_slots[i];
        if ( !slot.fence ) continue;
        const GLenum state = gl->glClientWaitSync(slot.fence, 0, 0);
        if ( state == GL_TIMEOUT_EXPIRED ) continue;
        // A read-back issued before the last invalidate() (stop, seek, clip or mode change) is freed unread.
        // r6 (adversary r5 A1 on 9570ccf8): only a read-back NEWER than the last one published is published -- the older
        // slot can signal after the newer one was taken (a TIMEOUT on one poll, ALREADY_SIGNALED on the next), and it must
        // not move the scope backward. Serials only grow (never reset), so this holds across invalidate() too.
        if ( ( state == GL_ALREADY_SIGNALED || state == GL_CONDITION_SATISFIED ) && slot.generation == m_generation
          && slot.serial > m_resultSerial )
        {
            QElapsedTimer mapClock;
            mapClock.start();
            const int bytes = slot.width * slot.height * 4;
            gl->glBindBuffer(GL_PIXEL_PACK_BUFFER, slot.pbo);
            const void *mapped = gl->glMapBufferRange(GL_PIXEL_PACK_BUFFER, 0, bytes, GL_MAP_READ_BIT);
            std::vector<uint8_t> converted;
            if ( mapped )
                playbackScopeTapRgbaBottomUpToRgb8(static_cast<const uint8_t *>(mapped), slot.width, slot.height, &converted);
            // r6 (adversary r5 A3): GL_FALSE from glUnmapBuffer means the store was corrupted while mapped, so what was read
            // is not the picture: nothing is published or counted completed.
            const bool unmapped = mapped && gl->glUnmapBuffer(GL_PIXEL_PACK_BUFFER) == GL_TRUE;
            if ( unmapped )
            {
                m_result.swap(converted);
                m_resultWidth = slot.width;
                m_resultHeight = slot.height;
                m_resultSerial = slot.serial;
                m_resultReady = true;
                ++m_counters.completed;
                m_counters.lastWidth = slot.width;
                m_counters.lastHeight = slot.height;
                m_counters.mapMsTotal += static_cast<double>(mapClock.nsecsElapsed()) / 1.0e6;
                m_consecutiveGlFailures = 0;   // r4: a published read-back ends a failure run
            }
            else
            {
                noteGlFailure("map", false);   // r4 (A2 siblings :207/:213): a map that keeps failing falls back too
            }
            gl->glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
        }
        else if ( state == GL_WAIT_FAILED )
        {
            noteGlFailure("fence_wait", false);
        }
        // Signalled (mapped, stale or not) or GL_WAIT_FAILED: this read-back is finished either way.
        gl->glDeleteSync(slot.fence);
        slot.fence = nullptr;
    }
}

bool GpuScopeTap::take(std::vector<uint8_t> *rgb8, int *width, int *height, quint64 *serial)
{
    if ( !rgb8 || !m_resultReady ) return false;
    rgb8->swap(m_result);
    m_result.clear();
    if ( width ) *width = m_resultWidth;
    if ( height ) *height = m_resultHeight;
    if ( serial ) *serial = m_resultSerial;
    m_resultReady = false;
    return true;
}

void GpuScopeTap::invalidate()
{
    ++m_generation;
    m_requested = false;
    m_resultReady = false;
    m_result.clear();
}

void GpuScopeTap::destroy(bool contextCurrent)
{
    QOpenGLContext *current = QOpenGLContext::currentContext();
    const bool callGl = contextCurrent && m_namesContext && current == m_namesContext;
    QOpenGLExtraFunctions *gl = callGl ? current->extraFunctions() : nullptr;
    for ( Slot &slot : m_slots )
    {
        if ( gl && slot.fence ) gl->glDeleteSync(slot.fence);
        if ( gl && slot.pbo ) gl->glDeleteBuffers(1, &slot.pbo);
        slot = Slot();
    }
    if ( gl && m_fbo ) gl->glDeleteFramebuffers(1, &m_fbo);
    if ( gl && m_color ) gl->glDeleteRenderbuffers(1, &m_color);
    m_fbo = 0;
    m_color = 0;
    m_fboWidth = 0;
    m_fboHeight = 0;
    m_namesContext = nullptr;
    m_requested = false;
    m_resultReady = false;
    m_result.clear();
    m_drawnRectCapturable = false;
    m_supportContext = nullptr;
    m_supportCached = false;
    m_consecutiveGlFailures = 0;   // r4: a new context gets a fresh tap
    m_unusable = false;
    m_counters.presenterId = nextScopeTapPresenterId();   // r6 (Dot I1): a re-created context is another presenter
}
