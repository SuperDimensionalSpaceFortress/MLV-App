/*!
 * \file PlaybackFirstPresentIdentity.h
 * \brief Is the first Play-present the paused start frame? (pure, unit-tested)
 *
 * PLAYBACK-STARTUP-WAIT-PREWARM-1: a faster first present must still show the start frame, and never an unfilled
 * texture. The smoke's opt-in identity capture grabs the paused start frame before the measured Play and the
 * first present right after its swap; this compares them. Pass = the first present is not blank (std >= 1 over
 * its RGB channel values), its mean absolute difference from the pre-Play grab is <= 6.0 (the
 * PLAYBACK-VISUAL-INSPECTION-1 threshold: the paused and playback render paths are allowed to differ that much),
 * and the app-reported display frame is the start frame or the one after it.
 */

#ifndef PLAYBACKFIRSTPRESENTIDENTITY_H
#define PLAYBACKFIRSTPRESENTIDENTITY_H

#include <QImage>

#include <cmath>

namespace playback_first_present_identity
{

inline constexpr double kMinStd = 1.0;
inline constexpr double kMaxMeanAbsDiff = 6.0;

struct Stats
{
    bool comparable = false;   // both images present and the same size
    double firstStd = 0.0;
    double meanAbsDiff = -1.0;
    bool frameOk = false;
    bool pass = false;
};

/*! Standard deviation of every R, G and B value of \a image (0 for a null image). */
inline double rgbStd( const QImage &image )
{
    if( image.isNull() ) return 0.0;
    const QImage rgb = image.convertToFormat( QImage::Format_RGB32 );
    double sum = 0.0;
    double sumSq = 0.0;
    double n = 0.0;
    for( int y = 0; y < rgb.height(); ++y )
    {
        const QRgb *row = reinterpret_cast<const QRgb *>( rgb.constScanLine( y ) );
        for( int x = 0; x < rgb.width(); ++x )
        {
            const double c[3] = { double( qRed( row[x] ) ), double( qGreen( row[x] ) ), double( qBlue( row[x] ) ) };
            for( double v : c ) { sum += v; sumSq += v * v; }
            n += 3.0;
        }
    }
    if( n <= 0.0 ) return 0.0;
    const double mean = sum / n;
    const double var = sumSq / n - mean * mean;
    return var > 0.0 ? std::sqrt( var ) : 0.0;
}

/*! Mean absolute difference over every R, G and B value; -1 when the images are not comparable. */
inline double meanAbsDiff( const QImage &a, const QImage &b )
{
    if( a.isNull() || b.isNull() || a.size() != b.size() ) return -1.0;
    const QImage ra = a.convertToFormat( QImage::Format_RGB32 );
    const QImage rb = b.convertToFormat( QImage::Format_RGB32 );
    double sum = 0.0;
    double n = 0.0;
    for( int y = 0; y < ra.height(); ++y )
    {
        const QRgb *pa = reinterpret_cast<const QRgb *>( ra.constScanLine( y ) );
        const QRgb *pb = reinterpret_cast<const QRgb *>( rb.constScanLine( y ) );
        for( int x = 0; x < ra.width(); ++x )
        {
            sum += std::abs( qRed( pa[x] ) - qRed( pb[x] ) ) + std::abs( qGreen( pa[x] ) - qGreen( pb[x] ) )
                 + std::abs( qBlue( pa[x] ) - qBlue( pb[x] ) );
            n += 3.0;
        }
    }
    return n > 0.0 ? sum / n : -1.0;
}

inline Stats compare( const QImage &prePlay, const QImage &firstPresent, int startFrame, int firstPresentFrame )
{
    Stats s;
    s.firstStd = rgbStd( firstPresent );
    s.meanAbsDiff = meanAbsDiff( prePlay, firstPresent );
    s.comparable = s.meanAbsDiff >= 0.0;
    s.frameOk = startFrame >= 0 && ( firstPresentFrame == startFrame || firstPresentFrame == startFrame + 1 );
    s.pass = s.comparable && s.firstStd >= kMinStd && s.meanAbsDiff <= kMaxMeanAbsDiff && s.frameOk;
    return s;
}

} // namespace playback_first_present_identity

#endif // PLAYBACKFIRSTPRESENTIDENTITY_H
