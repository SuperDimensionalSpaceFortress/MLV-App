/*!
 * \file LookLabelOverlay.h
 * \brief LOOK-FULLSCREEN-PARITY-1: rasterises the fullscreen "current look" label once per change.
 *        Both fullscreen present paths (GpuDisplayWindow's swapchain and GraphicsZoomView's CPU
 *        viewport) cache the image this returns and only composite it per frame.
 */

#ifndef LOOKLABELOVERLAY_H
#define LOOKLABELOVERLAY_H

#include <QImage>
#include <QString>

/*! \brief White text on a translucent dark rounded backing, premultiplied ARGB32, in device pixels.
 *  The backing keeps it readable on bright frames and the soft edge on dark ones. `fontPx` is the
 *  text height (look_label::fontPixels). A null image for empty text. */
QImage renderLookLabelOverlayImage( const QString &text, int fontPx );

#endif // LOOKLABELOVERLAY_H
