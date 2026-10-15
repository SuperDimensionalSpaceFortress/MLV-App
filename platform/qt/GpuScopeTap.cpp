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

GpuScopeTap::GpuScopeTap(const char *surfaceName)
    : m_surfaceName(surfaceName ? surfaceName : "unknown")
{
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
    QOpenGLExtraFunctions *gl = ( context && QOpenGLContext::currentContext() == context ) ? context->extraFunctions() : nullptr;
    QRect source;
    if ( !gl
         || !supportedCached(context)
         || ( m_namesContext && m_namesContext != context )
         || !playbackScopeTapFramebufferRect(leftPx, topPx, rightPx, bottomPx, fbWidth, fbHeight, &source) )
    {
        ++m_counters.skipped;
        return;
    }
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
    m_namesContext = context;
    const QSize tapSize = playbackScopeTapSize(source.width(), source.height(), m_maxWidth);

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
        gl->glBindFramebuffer(GL_FRAMEBUFFER, sourceFbo);
        if ( status != GL_FRAMEBUFFER_COMPLETE )
        {
            gl->glDeleteFramebuffers(1, &m_fbo);
            gl->glDeleteRenderbuffers(1, &m_color);
            m_fbo = 0;
            m_color = 0;
            m_fboWidth = 0;
            m_fboHeight = 0;
            ++m_counters.skipped;
            return;
        }
        m_fboWidth = tapSize.width();
        m_fboHeight = tapSize.height();
    }

    Slot &slot = m_slots[slotIndex];
    const int bytes = tapSize.width() * tapSize.height() * 4;
    if ( !slot.pbo ) gl->glGenBuffers(1, &slot.pbo);

    gl->glBindFramebuffer(GL_READ_FRAMEBUFFER, sourceFbo);
    gl->glBindFramebuffer(GL_DRAW_FRAMEBUFFER, m_fbo);
    gl->glBlitFramebuffer(source.left(), source.top(), source.left() + source.width(), source.top() + source.height(),
                          0, 0, tapSize.width(), tapSize.height(), GL_COLOR_BUFFER_BIT, GL_LINEAR);
    gl->glBindFramebuffer(GL_READ_FRAMEBUFFER, m_fbo);
    gl->glBindBuffer(GL_PIXEL_PACK_BUFFER, slot.pbo);
    if ( slot.capacityBytes < bytes )
    {
        gl->glBufferData(GL_PIXEL_PACK_BUFFER, bytes, nullptr, GL_STREAM_READ);
        slot.capacityBytes = bytes;
    }
    gl->glPixelStorei(GL_PACK_ALIGNMENT, 4);
    // Into the bound PBO (offset 0): the GPU copies asynchronously; nothing reaches client memory here.
    gl->glReadPixels(0, 0, tapSize.width(), tapSize.height(), GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    gl->glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
    slot.fence = gl->glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
    gl->glBindFramebuffer(GL_FRAMEBUFFER, sourceFbo);
    if ( !slot.fence )
    {
        ++m_counters.skipped;
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
    QOpenGLExtraFunctions *gl = context->extraFunctions();
    if ( !gl ) return;

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
        if ( ( state == GL_ALREADY_SIGNALED || state == GL_CONDITION_SATISFIED ) && slot.generation == m_generation )
        {
            QElapsedTimer mapClock;
            mapClock.start();
            const int bytes = slot.width * slot.height * 4;
            gl->glBindBuffer(GL_PIXEL_PACK_BUFFER, slot.pbo);
            const void *mapped = gl->glMapBufferRange(GL_PIXEL_PACK_BUFFER, 0, bytes, GL_MAP_READ_BIT);
            if ( mapped )
            {
                playbackScopeTapRgbaBottomUpToRgb8(static_cast<const uint8_t *>(mapped), slot.width, slot.height, &m_result);
                gl->glUnmapBuffer(GL_PIXEL_PACK_BUFFER);
                m_resultWidth = slot.width;
                m_resultHeight = slot.height;
                m_resultSerial = slot.serial;
                m_resultReady = true;
                ++m_counters.completed;
                m_counters.lastWidth = slot.width;
                m_counters.lastHeight = slot.height;
                m_counters.mapMsTotal += static_cast<double>(mapClock.nsecsElapsed()) / 1.0e6;
            }
            gl->glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
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
}
