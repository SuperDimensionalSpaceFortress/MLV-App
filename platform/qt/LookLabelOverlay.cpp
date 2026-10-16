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
    const int textWidth = metrics.horizontalAdvance( text );
    const QSize size( textWidth + 2 * padX, metrics.height() + 2 * padY );

    QImage image( size, QImage::Format_ARGB32_Premultiplied );
    image.fill( Qt::transparent );
    QPainter painter( &image );
    painter.setRenderHint( QPainter::Antialiasing, true );
    painter.setRenderHint( QPainter::TextAntialiasing, true );

    const qreal radius = size.height() * 0.28;
    QPainterPath backing;
    backing.addRoundedRect( QRectF( 0.5, 0.5, size.width() - 1.0, size.height() - 1.0 ), radius, radius );
    painter.fillPath( backing, QColor( 0, 0, 0, 140 ) );
    painter.setPen( QColor( 255, 255, 255, 40 ) );
    painter.drawPath( backing );

    painter.setFont( font );
    const QRect textRect( padX, padY, textWidth, metrics.height() );
    // A one-pixel shadow under the text: legible even where the backing meets a bright edge.
    painter.setPen( QColor( 0, 0, 0, 160 ) );
    painter.drawText( textRect.translated( 0, qMax( 1, fontPx / 20 ) ), Qt::AlignLeft | Qt::AlignVCenter, text );
    painter.setPen( QColor( 255, 255, 255, 235 ) );
    painter.drawText( textRect, Qt::AlignLeft | Qt::AlignVCenter, text );
    painter.end();
    return image;
}
