// LOOK-ASSIST-FILM-FLAVOR-1: a receipt gradationCurve string pushed into a processing object the way the Curves widget
// pushes it (Curves::setConfiguration -> paintElement -> processingSetGCurve per line: Y = 0, R = 1, G = 2, B = 3), so a
// test can build the engine's four 65536-entry tables from a receipt string.
#ifndef LOOK_ASSIST_GRADATION_CURVE_H
#define LOOK_ASSIST_GRADATION_CURVE_H

#include "../../src/batch/LookAssistAnalysis.h"
#include "../../src/mlv_include.h"

#include <QString>

#include <algorithm>
#include <cstdint>
#include <vector>

inline void lookAssistTestApplyGradationCurve( processingObject_t *processing, const QString &curve )
{
    std::vector<lookassist::LookAssistGradationPoint> lines[4];
    bool wellFormed = false;
    // Curves::setConfiguration pushes no table when the string runs out before its fourth line (adversary A3 on 76653777);
    // an x with no y or a token that is no number pushes nothing either (EXPORT-PRORES-LOOK-ASSIST-1 r3, r2 A4).
    if( lookassist::lookAssistParseGradationCurve( curve, lines, &wellFormed ) != 4 || !wellFormed ) return;
    for( const std::vector<lookassist::LookAssistGradationPoint> &line : lines )   // adversary A2 on 16d54744
        if( line.size() < 2 ) return;
    // All four channels or none, as ReceiptApplier::applyLookAssistGradeToProcessing pushes them (r3, r2 A5).
    uint16_t *tables[4] = { processing->gcurve_y, processing->gcurve_r, processing->gcurve_g, processing->gcurve_b };
    std::vector<uint16_t> snapshot[4];
    for( int channel = 0; channel < 4; ++channel )
        snapshot[channel].assign( tables[channel], tables[channel] + 65536 );
    for( int channel = 0; channel < 4; ++channel )
    {
        // Copies: the spline helper nudges equal x values in place, as it does on the widget's own arrays.
        std::vector<float> xs, ys;
        for( const lookassist::LookAssistGradationPoint &point : lines[channel] )
        {
            xs.push_back( point.x );
            ys.push_back( point.y );
        }
        if( processingSetGCurve( processing, static_cast<int>( xs.size() ), xs.data(), ys.data(),
                                 static_cast<uint8_t>( channel ) ) != 0 )
        {
            for( int restore = 0; restore < 4; ++restore )
                std::copy( snapshot[restore].begin(), snapshot[restore].end(), tables[restore] );
            return;
        }
    }
}

#endif
