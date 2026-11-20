/*!
 * \file FullscreenControlBarPolicy.h
 * \brief PLAYER-FULLSCREEN-CONTROL-BAR-1: the fullscreen control bar's pure seam. Layout, hit test, seek mapping and
 *        the Presenter that decides when the bar is rasterised. Header-only, QtCore/QtGui value types only, no moc,
 *        so console_tests pins it without a GUI.
 *
 * The bar is a strip along the bottom of the fullscreen viewer: play/pause, previous frame, next frame, a seek
 * track, the timecode and exit fullscreen. It shows on mouse activity and hides with the cursor on #855's one idle
 * clock (viewer_idle::Controller, ViewerIdlePolicy.h); there is no second timer. It is held shown while the pointer
 * is over it or a seek drag is in progress (viewer_idle::Inputs::overlayHeld).
 *
 * Zero cost while hidden: noteFeed() is a no-op while hidden, and rasterFor() asks for no raster while hidden, so a
 * surface that follows it draws, rasterises and uploads nothing. While shown, the static chrome (background, icons,
 * the play/pause state) is rasterised once per bar size and play state together with the timecode (Raster::Full);
 * afterwards only a changed timecode string is rasterised (Raster::Timecode), into the same image. The playhead is
 * never rasterised: the surface draws it as one rectangle at playheadRect().
 *
 * All rects are device pixels with a top-left origin on the surface (the GL framebuffer, or the CPU viewer's
 * device-pixel size).
 */

#ifndef FULLSCREENCONTROLBARPOLICY_H
#define FULLSCREENCONTROLBARPOLICY_H

#include <QEvent>
#include <QPoint>
#include <QPointF>
#include <QRect>
#include <QSize>
#include <QString>
#include <QtGlobal>
#include <QtMath>

namespace fullscreen_bar
{

/*! \brief The bar's parts. None is outside the bar; Bar is inside it but on no control (still claimed). */
enum class Part { None, Bar, Play, Prev, Next, Seek, Timecode, Exit };

/*! \brief Bar height in device pixels for a surface this many device pixels high: about 1/18 of it (60 px on a
 *         1080-line panel, 120 on 2160), never under 28 or over 120. */
inline int barHeightPixels( int surfaceHeightPx )
{
    return qBound( 28, qRound( surfaceHeightPx / 18.0 ), 120 );
}

/*! \brief Where everything sits, device pixels. track is the drawn seek line inside the seek column. */
struct Layout
{
    QRect bar;
    QRect play;
    QRect prev;
    QRect next;
    QRect seek;       // the seek hit column (full bar height)
    QRect track;      // the drawn seek line, centred in the column
    QRect timecode;
    QRect exit;
    int fontPx = 0;
    int pad = 0;
};

/*! \brief The layout for a surface of fbDevicePx. Empty (bar.isEmpty()) for an empty surface. Buttons are the bar's
 *         height tall; they are square (bar height wide) only when the surface is at least 4x the bar height wide,
 *         otherwise each is a quarter of the surface width wide. From the left: Play, Prev, Next, the seek column; from
 *         the right: Exit, then the timecode. On a surface too narrow for a seek column the seek rects stay empty. */
inline Layout layoutFor( const QSize &fbDevicePx )
{
    Layout l;
    if( fbDevicePx.width() <= 0 || fbDevicePx.height() <= 0 ) return l;
    const int h = qMin( barHeightPixels( fbDevicePx.height() ), fbDevicePx.height() );
    const int top = fbDevicePx.height() - h;
    const int w = fbDevicePx.width();
    l.bar = QRect( 0, top, w, h );
    l.pad = qMax( 2, h / 6 );
    l.fontPx = qBound( 10, qRound( h * 0.4 ), 48 );
    const int btn = qMin( h, w / 4 );
    if( btn <= 0 ) return l;
    l.play = QRect( 0, top, btn, h );
    l.prev = QRect( btn, top, btn, h );
    l.next = QRect( 2 * btn, top, btn, h );
    l.exit = QRect( w - btn, top, btn, h );
    // "00:00:00:00" is 11 glyphs; a digit is about 0.6 em, a colon less.
    const int tcWidth = qRound( l.fontPx * 6.6 ) + 2 * l.pad;
    const int tcLeft = qMax( 3 * btn, l.exit.left() - tcWidth );
    l.timecode = QRect( tcLeft, top, l.exit.left() - tcLeft, h );
    const int seekLeft = 3 * btn + l.pad;
    const int seekRight = l.timecode.left() - l.pad;   // exclusive
    if( seekRight - seekLeft >= 2 )
    {
        l.seek = QRect( seekLeft, top, seekRight - seekLeft, h );
        const int th = qMin( h, qMax( 2, h / 12 ) );   // never taller than the bar (a 1-line surface)
        l.track = QRect( seekLeft, top + ( h - th ) / 2, seekRight - seekLeft, th );
    }
    return l;
}

/*! \brief Which part of the bar a device-pixel position is on. None outside the bar. */
inline Part hitTest( const Layout &l, const QPointF &devicePos )
{
    const QPoint p( qFloor( devicePos.x() ), qFloor( devicePos.y() ) );
    if( l.bar.isEmpty() || !l.bar.contains( p ) ) return Part::None;
    if( l.play.contains( p ) ) return Part::Play;
    if( l.prev.contains( p ) ) return Part::Prev;
    if( l.next.contains( p ) ) return Part::Next;
    if( l.exit.contains( p ) ) return Part::Exit;
    if( !l.seek.isEmpty() && l.seek.contains( p ) ) return Part::Seek;
    if( l.timecode.contains( p ) ) return Part::Timecode;
    return Part::Bar;
}

/*! \brief The frame (0 .. totalFrames - 1) at device x along the seek column, clamped at both ends. 0 when there
 *         is at most one frame or no seek column. */
inline int seekFrameAt( const Layout &l, qreal deviceX, int totalFrames )
{
    if( totalFrames <= 1 || l.seek.width() < 2 ) return 0;
    const qreal span = qreal( l.seek.width() - 1 );
    qreal f = ( deviceX - qreal( l.seek.left() ) ) / span;
    if( !( f > 0.0 ) ) f = 0.0;   // also NaN
    if( f > 1.0 ) f = 1.0;
    return qBound( 0, qRound( f * qreal( totalFrames - 1 ) ), totalFrames - 1 );
}

/*! \brief The playhead marker for frame of totalFrames: a narrow upright rect centred on the frame's x in the seek
 *         column, kept inside the column. Empty without a seek column. */
inline QRect playheadRect( const Layout &l, int frame, int totalFrames )
{
    if( l.seek.width() < 2 ) return QRect();
    // Never wider than the seek column (a tall narrow surface) nor taller than the bar (a few-line surface), so the
    // clamp below always has a valid range.
    const int w = qMin( l.seek.width(), qMax( 2, l.bar.height() / 10 ) );
    const int h = qMin( l.bar.height(), qMax( 4, l.bar.height() / 2 ) );
    qreal f = 0.0;
    if( totalFrames > 1 ) f = qBound( 0.0, qreal( frame ) / qreal( totalFrames - 1 ), 1.0 );
    const int cx = l.seek.left() + qRound( f * qreal( l.seek.width() - 1 ) );
    int x = cx - w / 2;
    x = qBound( l.seek.left(), x, l.seek.left() + l.seek.width() - w );
    return QRect( x, l.bar.top() + ( l.bar.height() - h ) / 2, w, h );
}

/*! \brief Whether a mouse event the idle clock counted as activity also shows the bar. A press, release, double-click
 *         or wheel does. A move does once the pointer has left its position at fullscreen entry (2 logical pixels,
 *         Manhattan): Windows synthesizes a move at an unchanged position when the window changes under a still
 *         pointer, and the first move after entry has no last position to compare with, so the idle clock counts it.
 *         That is not a request for the bar. Once a move left the entry position (*sawMove), every move shows it. */
inline bool activityShowsBar( QEvent::Type type, bool *sawMove, const QPoint &entryPos, const QPoint &cursorNow )
{
    if( type != QEvent::MouseMove ) return true;
    if( *sawMove ) return true;
    if( ( cursorNow - entryPos ).manhattanLength() < 2 ) return false;
    *sawMove = true;
    return true;
}

/*! \brief What a mouse event on the bar does. */
enum class Action { None, TogglePlay, PrevFrame, NextFrame, ExitFullscreen, Seek };

/*! \brief The bar's mouse state between events: the buttons whose press the bar took (each one's release is the bar's
 *         too, wherever it lands) and a seek drag in progress. Per button, so a button pressed elsewhere keeps its own
 *         release (adversary r1 pass 2 A3). */
struct InputState
{
    Qt::MouseButtons owned = Qt::NoButton;
    bool seekDrag = false;
};

/*! \brief The held buttons that belong to a gesture begun elsewhere: every held button other than this event's own
 *         button (a press's; NoButton for a wheel) that the bar did not take. The event's own button never counts as
 *         the bar's: a press of a button the bar still owns means its release was lost, so that stale ownership is no
 *         evidence the other held buttons are the bar's (adversary r1 pass 3 A3). */
inline Qt::MouseButtons foreignButtons( const InputState &s, Qt::MouseButton button, Qt::MouseButtons buttons )
{
    return buttons & ~Qt::MouseButtons( button ) & ~s.owned;
}

/*! \brief Whether the bar takes this event (the viewer then never sees it). button is the event's own button (a
 *         press's, a release's; NoButton for a move or wheel); part is hitTest at the event position. Press and
 *         double-click: the shown bar takes them inside it only if every OTHER held button is one the bar took (a
 *         gesture begun elsewhere keeps every event). Wheel: inside the shown bar only with no button held that the bar
 *         did not take. Move: during the bar's seek drag, while only buttons the bar took are held, or inside the shown
 *         bar when no button is held (a viewer drag that crosses the bar keeps its moves). Release: iff a button the
 *         bar took is no longer held, so a press elsewhere is always released elsewhere. */
inline bool claims( const InputState &s, bool shown, QEvent::Type type, Qt::MouseButton button, Qt::MouseButtons buttons,
                    Part part )
{
    switch( type )
    {
    case QEvent::MouseButtonPress:
    case QEvent::MouseButtonDblClick:
        return shown && part != Part::None && foreignButtons( s, button, buttons ) == Qt::NoButton;
    case QEvent::Wheel:
        return shown && part != Part::None && foreignButtons( s, Qt::NoButton, buttons ) == Qt::NoButton;
    case QEvent::MouseMove:
        if( s.seekDrag ) return true;
        if( s.owned != Qt::NoButton && ( buttons & ~s.owned ) == Qt::NoButton ) return true;
        return shown && part != Part::None && buttons == Qt::NoButton;
    case QEvent::MouseButtonRelease:
        return ( s.owned & ~buttons ) != Qt::NoButton;
    default:
        return false;
    }
}

/*! \brief The action of a claimed event, updating the state. A left press (or double-click: a widget gets it in
 *         place of the second press) acts at once: a button fires, the seek column starts a drag and seeks. A move
 *         during the drag seeks while the left button is held. A release ends the drag and that button's ownership. */
inline Action onMouse( InputState *s, QEvent::Type type, Qt::MouseButton button, Qt::MouseButtons buttons, Part part )
{
    switch( type )
    {
    case QEvent::MouseButtonPress:
    case QEvent::MouseButtonDblClick:
        s->owned |= button;
        if( button != Qt::LeftButton ) return Action::None;
        switch( part )
        {
        case Part::Play: return Action::TogglePlay;
        case Part::Prev: return Action::PrevFrame;
        case Part::Next: return Action::NextFrame;
        case Part::Exit: return Action::ExitFullscreen;
        case Part::Seek:
            s->seekDrag = true;
            return Action::Seek;
        default: return Action::None;
        }
    case QEvent::MouseMove:
        if( s->seekDrag )
        {
            if( buttons & Qt::LeftButton ) return Action::Seek;
            s->seekDrag = false;   // the release was lost (grab broken): the drag is over
        }
        return Action::None;
    case QEvent::MouseButtonRelease:
        if( button == Qt::LeftButton ) s->seekDrag = false;
        s->owned &= ~Qt::MouseButtons( button );
        if( buttons == Qt::NoButton ) s->owned = Qt::NoButton;
        return Action::None;
    default:
        return Action::None;
    }
}

/*! \brief The action to perform for a taken event. A widget receives a native double-click as Press, Release,
 *         DblClick, Release (QWidgetWindow drops the press that created the double-click, QTBUG-25831); a QWindow (the
 *         GL path) receives that press too, flagged Qt::MouseEventCreatedDoubleClick, then the DblClick. The DblClick
 *         acts on both paths, so that flagged press is taken (its ownership kept) but does not act: each physical
 *         click acts once, Next steps twice, never three times (adversary r1 pass 2 A1). */
inline Action actionOf( Action action, QEvent::Type type, bool pressCreatedDoubleClick )
{
    return ( type == QEvent::MouseButtonPress && pressCreatedDoubleClick ) ? Action::None : action;
}

/*! \brief One mouse event the GL path's filter may give the bar: settles stale state, then claims() and onMouse().
 *         Stale state first: an owned button no longer held (on a press or move) was released where the bar never
 *         saw it (e.g. after the window changed under it), and a press or double-click of a button the bar still owns
 *         means that button's release was lost. Either clears that ownership (per button) and seekDrag, so a lost
 *         release can never make the bar take the viewer's later gestures or hold the bar shown. Returns whether the
 *         bar took the event; *action is its action. */
inline bool route( InputState *s, bool shown, QEvent::Type type, Qt::MouseButton button, Qt::MouseButtons buttons, Part part,
                   Action *action )
{
    *action = Action::None;
    const bool press = type == QEvent::MouseButtonPress || type == QEvent::MouseButtonDblClick;
    if( press || type == QEvent::MouseMove )
    {
        // An owned button no longer held was released where the bar never saw it.
        s->owned &= buttons;
        if( !( buttons & Qt::LeftButton ) ) s->seekDrag = false;
    }
    // An owned button pressed again: its own release was lost. That ownership is settled before the claim is judged
    // (adversary r1 pass 3 A3), so the new press is the bar's again only on its own merits.
    if( press && ( s->owned & button ) )
    {
        s->owned &= ~Qt::MouseButtons( button );
        if( button == Qt::LeftButton ) s->seekDrag = false;
    }
    if( !claims( *s, shown, type, button, buttons, part ) ) return false;
    *action = onMouse( s, type, button, buttons, part );
    return true;
}

/*! \brief What the bar shows: position, length, timecode text and the play state. */
struct Feed
{
    int frame = 0;
    int totalFrames = 0;
    QString timecode;
    bool playing = false;
};

inline bool operator==( const Feed &a, const Feed &b )
{
    return a.frame == b.frame && a.totalFrames == b.totalFrames && a.playing == b.playing && a.timecode == b.timecode;
}

inline bool operator!=( const Feed &a, const Feed &b ) { return !( a == b ); }

/*! \brief What the next draw must rasterise. */
enum class Raster { None, Full, Timecode };

/*! \brief One surface's bar state: shown or not, the latest feed, and the raster cache key. Counts every raster and
 *         upload the surface reports, every show, and the time shown (milliseconds on the caller's clock). */
class Presenter
{
public:
    bool shown() const { return m_shown; }

    /*! \brief Shows the bar. True iff it was hidden. */
    bool show( qint64 nowMs )
    {
        if( m_shown ) return false;
        m_shown = true;
        m_shownSince = nowMs;
        ++m_shows;
        return true;
    }

    /*! \brief Hides the bar. True iff it was shown. The raster cache is kept (a re-show with the same state
     *         rasterises nothing). */
    bool hide( qint64 nowMs )
    {
        if( !m_shown ) return false;
        m_shown = false;
        if( nowMs > m_shownSince ) m_shownMsTotal += nowMs - m_shownSince;
        return true;
    }

    /*! \brief The latest values. A no-op returning false while hidden; true iff something shown changed. */
    bool noteFeed( const Feed &feed )
    {
        if( !m_shown ) return false;
        if( feed == m_feed ) return false;
        m_feed = feed;
        return true;
    }

    const Feed &feed() const { return m_feed; }

    /*! \brief True iff the bar is shown and this timecode is not the one last rasterised. */
    bool needsRaster( const QString &timecode ) const
    {
        return m_shown && ( !m_haveRaster || timecode != m_rasterTimecode );
    }

    /*! \brief What a draw of a bar of barSize must rasterise now. None while hidden or for an empty bar. Full on the
     *         first draw, a size change, a play-state change, or after dropRaster(); Timecode when only the
     *         timecode string changed. */
    Raster rasterFor( const QSize &barSize ) const
    {
        if( !m_shown || barSize.isEmpty() ) return Raster::None;
        if( !m_haveRaster || barSize != m_rasterSize || m_feed.playing != m_rasterPlaying ) return Raster::Full;
        return needsRaster( m_feed.timecode ) ? Raster::Timecode : Raster::None;
    }

    /*! \brief The surface rasterised r for barSize from feed(). */
    void noteRaster( Raster r, const QSize &barSize )
    {
        if( r == Raster::None ) return;
        ++m_rasterCount;
        m_haveRaster = true;
        m_rasterSize = barSize;
        m_rasterPlaying = m_feed.playing;
        m_rasterTimecode = m_feed.timecode;
    }

    /*! \brief The surface uploaded a raster (a texture upload or a pixmap conversion). */
    void noteUpload() { ++m_uploadCount; }

    /*! \brief The surface lost its raster (GL context gone): the next draw is Full. */
    void dropRaster() { m_haveRaster = false; }

    int rasterCount() const { return m_rasterCount; }
    int uploadCount() const { return m_uploadCount; }
    int shows() const { return m_shows; }
    qint64 shownMs( qint64 nowMs ) const
    {
        return m_shownMsTotal + ( m_shown && nowMs > m_shownSince ? nowMs - m_shownSince : 0 );
    }

private:
    bool m_shown = false;
    qint64 m_shownSince = 0;
    qint64 m_shownMsTotal = 0;
    int m_shows = 0;
    Feed m_feed;
    bool m_haveRaster = false;
    QSize m_rasterSize;
    bool m_rasterPlaying = false;
    QString m_rasterTimecode;
    int m_rasterCount = 0;
    int m_uploadCount = 0;
};

/*! \brief The one telemetry line, emitted on fullscreen exit and at shutdown when not yet emitted. */
inline QString summaryLine( int raster, int upload, qint64 shownMs, int shows )
{
    return QStringLiteral( "control_bar.summary raster=%1 upload=%2 shown_ms=%3 shows=%4" )
        .arg( raster ).arg( upload ).arg( shownMs ).arg( shows );
}

} // namespace fullscreen_bar

#endif // FULLSCREENCONTROLBARPOLICY_H
