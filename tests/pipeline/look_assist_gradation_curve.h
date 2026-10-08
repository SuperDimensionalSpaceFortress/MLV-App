// LOOK-ASSIST-FILM-FLAVOR-1: a receipt gradationCurve string pushed into a processing object the way the Curves widget
// pushes it (Curves::setConfiguration -> paintElement -> processingSetGCurve per line: Y = 0, R = 1, G = 2, B = 3), so a
// test can build the engine's four 65536-entry tables from a receipt string.
#ifndef LOOK_ASSIST_GRADATION_CURVE_H
#define LOOK_ASSIST_GRADATION_CURVE_H

#include "../../src/batch/LookAssistAnalysis.h"
#include "../../src/mlv_include.h"

#include <QString>

#include <cstdint>
#include <vector>

inline void lookAssistTestApplyGradationCurve( processingObject_t *processing, const QString &curve )
{
    std::vector<lookassist::LookAssistGradationPoint> lines[4];
    lookassist::lookAssistParseGradationCurve( curve, lines );
    for( int channel = 0; channel < 4; ++channel )
    {
        // Copies: the spline helper nudges equal x values in place, as it does on the widget's own arrays.
        std::vector<float> xs, ys;
        for( const lookassist::LookAssistGradationPoint &point : lines[channel] )
        {
            xs.push_back( point.x );
            ys.push_back( point.y );
        }
        processingSetGCurve( processing, static_cast<int>( xs.size() ), xs.data(), ys.data(),
                             static_cast<uint8_t>( channel ) );
    }
}

#endif
