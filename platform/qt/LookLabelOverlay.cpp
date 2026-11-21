/*!
 * \file LookLabelOverlay.cpp
 * \brief LOOK-FULLSCREEN-PARITY-1: the fullscreen look label's pixels (see header).
 */

#include "LookLabelOverlay.h"

#include <QColor>
#include <QFont>
#include <QFontMetrics>
#include <QPainter>
#include <QPainterPath>
#include <QStringList>
#include <QtGlobal>

QImage renderLookLabelOverlayImage( const QString &text, int fontPx )
{
    if( text.isEmpty() || fontPx <= 0 ) return QImage();

    QFont font( QStringLiteral( "Segoe UI" ) );
    font.setPixelSize( fontPx );
    font.setWeight( QFont::DemiBold );
    const QFontMetrics metrics( font );
    const int padX = qMax( 4, qRound( fontPx * 0.55 ) );
    const int padY = qMax( 2, qRound( fontPx * 0.30 ) );
    // LOOK-LABEL-PLAYBACK-INFO-1: one line per '\n' (the look line, then the playback-info lines), each right-aligned in
    // the widest line's width. One line is laid out exactly as before, so its pixels are unchanged.
    const QStringList lines = text.split( QLatin1Char( '\n' ) );
    int textWidth = 0;
    for( const QString &line : lines ) textWidth = qMax( textWidth, metrics.horizontalAdvance( line ) );
    const int lineStep = metrics.lineSpacing();
    const int textHeight = metrics.height() + ( lines.size() - 1 ) * lineStep;
    const QSize size( textWidth + 2 * padX, textHeight + 2 * padY );

    QImage image( size, QImage::Format_ARGB32_Premultiplied );
    image.fill( Qt::transparent );
    QPainter painter( &image );
    painter.setRenderHint( QPainter::Antialiasing, true );
    painter.setRenderHint( QPainter::TextAntialiasing, true );

    // The corner radius is the one-line label's, so a taller block keeps the same corners.
    const qreal radius = ( metrics.height() + 2 * padY ) * 0.28;
    QPainterPath backing;
    backing.addRoundedRect( QRectF( 0.5, 0.5, size.width() - 1.0, size.height() - 1.0 ), radius, radius );
    painter.fillPath( backing, QColor( 0, 0, 0, 140 ) );
    painter.setPen( QColor( 255, 255, 255, 40 ) );
    painter.drawPath( backing );

    painter.setFont( font );
    const int shadowDy = qMax( 1, fontPx / 20 );
    for( int i = 0; i < lines.size(); ++i )
    {
        const QString &line = lines.at( i );
        const int lineWidth = metrics.horizontalAdvance( line );
        const QRect textRect( padX + textWidth - lineWidth, padY + i * lineStep, lineWidth, metrics.height() );
        // A one-pixel shadow under the text: legible even where the backing meets a bright edge.
        painter.setPen( QColor( 0, 0, 0, 160 ) );
        painter.drawText( textRect.translated( 0, shadowDy ), Qt::AlignLeft | Qt::AlignVCenter, line );
        painter.setPen( QColor( 255, 255, 255, 235 ) );
        painter.drawText( textRect, Qt::AlignLeft | Qt::AlignVCenter, line );
    }
    painter.end();
    return image;
}
