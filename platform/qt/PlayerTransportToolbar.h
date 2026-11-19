// PLAYER-TOOLBAR-TRANSPORT-BUTTONS-1: the transport cluster on the main toolbar.
//
// clusterActionNames() is the ONE list of the cluster's action object names, in toolbar order
// (separators left out). MainWindow.ui's mainToolBar carries them in this order (pinned by
// tests/console/test_playback_fullscreen_ui_wiring.cpp), and PLAYER-FULLSCREEN-CONTROL-BAR-1
// reuses the list.
//
// finish() runs once at the end of MainWindow::initGui(), AFTER PlayerShortcutBinding::
// bindToWindow(): it gives the two icon-less actions (Goto Last Frame, Stop) a runtime icon and
// appends each toolbar action's live keys to its tooltip, so no key string is typed by hand.
//
// Header-only, no Q_OBJECT (no moc).
#pragma once

#include "PlaybackFrameRange.h"
#include "PlayerShortcutBinding.h"

#include <QAction>
#include <QColor>
#include <QIcon>
#include <QImage>
#include <QKeySequence>
#include <QList>
#include <QPainter>
#include <QPixmap>
#include <QString>
#include <QStringList>
#include <QToolBar>
#include <QVariant>
#include <QWidget>
#include <QWidgetAction>

namespace PlayerTransportToolbar
{

// The transport cluster, in toolbar order, separators left out.
inline QStringList clusterActionNames()
{
    return QStringList{
        QStringLiteral( "actionGoto_First_Frame" ),
        QStringLiteral( "actionPreviousFrame" ),
        QStringLiteral( "actionPlay" ),
        QStringLiteral( "actionStop" ),
        QStringLiteral( "actionNextFrame" ),
        QStringLiteral( "actionGoto_Last_Frame" ),
        QStringLiteral( "actionLoop" ),
        QStringLiteral( "actionAudioOutput" ),
        QStringLiteral( "actionZoomFit" ),
        QStringLiteral( "actionZoom100" ),
        QStringLiteral( "actionFullscreen" ) };
}

// "Jump to first frame..." + { Home, X } -> "Jump to first frame (Home or X)". No keys: base as it is.
inline QString keyedToolTip( const QString &base, const QList<QKeySequence> &keys )
{
    if( keys.isEmpty() ) return base;
    QString trimmed = base.trimmed();
    while( trimmed.endsWith( QLatin1Char( '.' ) ) || trimmed.endsWith( QChar( 0x2026 ) ) )
    {
        trimmed.chop( 1 );
        trimmed = trimmed.trimmed();
    }
    return QStringLiteral( "%1 (%2)" ).arg( trimmed, PlayerShortcutBinding::keysText( keys ) );
}

// Appends the live keys to the tooltip of every plain action on `bar`. Separators and widget
// actions (the playback-quality button, the timecode label) are left alone. The original tooltip
// is kept once in a dynamic property, so running this again gives the same result.
inline void applyKeyTooltips( QToolBar *bar )
{
    if( !bar ) return;
    const char *baseProperty = "playerTransportBaseToolTip";
    for( QAction *action : bar->actions() )
    {
        if( !action || action->isSeparator() || qobject_cast<QWidgetAction*>( action ) ) continue;
        if( !action->property( baseProperty ).isValid() )
            action->setProperty( baseProperty, action->toolTip() );
        const QString base = action->property( baseProperty ).toString();
        action->setToolTip( keyedToolTip( base, action->shortcuts() ) );
    }
}

// The image at `resource`, flipped horizontally (a null icon when it cannot be read).
inline QIcon mirroredIcon( const QString &resource )
{
    const QImage image( resource );
    if( image.isNull() ) return QIcon();
    return QIcon( QPixmap::fromImage( image.flipped( Qt::Horizontal ) ) );
}

// A Stop square the size of the Play image: transparent, with a centred filled square of 55% of
// the shorter side, in the colour of the Play image's most opaque pixel (first one in scan order).
inline QIcon stopIcon( const QString &playResource )
{
    const QImage play = QImage( playResource ).convertToFormat( QImage::Format_ARGB32 );
    if( play.isNull() || play.width() <= 0 || play.height() <= 0 ) return QIcon();
    QRgb ink = play.pixel( 0, 0 );
    for( int y = 0; y < play.height(); ++y )
        for( int x = 0; x < play.width(); ++x )
        {
            const QRgb pixel = play.pixel( x, y );
            if( qAlpha( pixel ) > qAlpha( ink ) ) ink = pixel;
        }
    QImage stop( play.size(), QImage::Format_ARGB32 );
    stop.fill( Qt::transparent );
    const int side = qMax( 1, ( qMin( play.width(), play.height() ) * 55 ) / 100 );
    QPainter painter( &stop );
    painter.fillRect( ( play.width() - side ) / 2, ( play.height() - side ) / 2, side, side,
                      QColor::fromRgba( ink ) );
    painter.end();
    return QIcon( QPixmap::fromImage( stop ) );
}

// The frame Stop returns to: cut in (1-based spin value) as a clamped 0-based frame index
// (a value at or below 0 is frame 0; the subtraction never overflows).
inline int stopTargetFrame( int cutInSpinValue, int totalFrames )
{
    return playback_frame_range::clampFrameIndex( cutInSpinValue <= 0 ? 0 : cutInSpinValue - 1, totalFrames );
}

// Runtime icons for the icon-less cluster actions, then the key tooltips. Must run after
// PlayerShortcutBinding::bindToWindow(), because the tooltips read the live shortcuts.
inline void finish( QToolBar *bar, QWidget *window,
                    const QString &firstFrameResource = QStringLiteral( ":/AudioVideoIcons/AudioVideoIcons/FirstFrame.png" ),
                    const QString &playResource = QStringLiteral( ":/AudioVideoIcons/AudioVideoIcons/Play.png" ) )
{
    if( window )
    {
        QAction *last = window->findChild<QAction*>( QStringLiteral( "actionGoto_Last_Frame" ), Qt::FindDirectChildrenOnly );
        if( last && last->icon().isNull() ) last->setIcon( mirroredIcon( firstFrameResource ) );
        QAction *stop = window->findChild<QAction*>( QStringLiteral( "actionStop" ), Qt::FindDirectChildrenOnly );
        if( stop && stop->icon().isNull() ) stop->setIcon( stopIcon( playResource ) );
    }
    applyKeyTooltips( bar );
}

} // namespace PlayerTransportToolbar
