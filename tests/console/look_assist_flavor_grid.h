// LOOK-ASSIST-FLAVORS-1: a deterministic grid of Look Assist statistics, run through a preset function.
//
// The grid only uses the API master already had (the four-argument presetForLookAssistScene), so the very same
// header compiles against master's source. The golden hash in test_look_assist_flavors.cpp was dumped from an
// UNCHANGED master tree (fork/master b5751928) with this header; the Classic flavor must reproduce it exactly.
#ifndef LOOK_ASSIST_FLAVOR_GRID_H
#define LOOK_ASSIST_FLAVOR_GRID_H

#include "../../src/batch/LookAssistAnalysis.h"

#include <QByteArray>
#include <QString>

#include <functional>
#include <vector>

namespace look_assist_flavor_grid
{

typedef std::function<lookassist::LookAssistPreset( lookassist::LookAssistScene,
                                                    const lookassist::LookAssistStats &,
                                                    const lookassist::LookAssistStats *,
                                                    const lookassist::LookAssistStats * )> PresetFn;

inline std::vector<lookassist::LookAssistStats> statsGrid()
{
    using lookassist::LookAssistStats;
    std::vector<LookAssistStats> grid;
    const double medians[] = { 18.0, 40.0, 75.0, 110.0, 150.0, 190.0 };
    const double p05Scale[] = { 0.9, 0.5, 0.25, 0.1 };
    const double p95Scale[] = { 1.1, 1.6, 2.0, 2.4 };
    const double p99Scale[] = { 1.2, 1.9, 2.4, 3.0 };
    const double dynamicRange[] = { 20.0, 90.0, 125.0, 200.0 };
    const double clipHigh[] = { 0.0, 0.015, 0.03 };
    // balance R/G/B: neutral, warm, cool, green; a negative index = no neutral samples at all.
    const double balance[4][3] = { { 100, 100, 100 }, { 130, 100, 70 }, { 70, 100, 130 }, { 90, 140, 90 } };
    for( double median : medians )
        for( int shape = 0; shape < 4; ++shape )
            for( double clip : clipHigh )
                for( int variant = -1; variant < 4; ++variant )
                {
                    LookAssistStats s;
                    s.median = median;
                    s.p05 = median * p05Scale[shape];
                    s.p95 = median * p95Scale[shape];
                    s.p99 = median * p99Scale[shape];
                    s.clipHigh = clip;
                    s.dynamicRange = dynamicRange[shape];
                    if( variant >= 0 )
                    {
                        s.balanceR = balance[variant][0];
                        s.balanceG = balance[variant][1];
                        s.balanceB = balance[variant][2];
                        s.balanceSamples = 200;
                    }
                    grid.push_back( s );
                }
    // The flat-floor thumbnail (the tracked fixtures' RAW thumbnail), with and without the rest of the evidence.
    for( int evidence = 0; evidence < 3; ++evidence )
    {
        LookAssistStats s;
        s.median = 37.0;
        s.p05 = 35.0;
        s.p95 = 37.0;
        s.p99 = 38.0;
        s.dynamicRange = 3.0;
        s.balanceR = 100.0;
        s.balanceG = 106.0;
        s.balanceB = 96.0;
        s.balanceSamples = 200;
        s.greenArtifactRatio = 0.01 * evidence;
        s.greenArtifactMeanAxis = 30.0 * evidence;
        s.daylightPictureEvidence = evidence == 2;
        s.hasSceneEv100 = evidence >= 1;
        s.sceneEv100 = 16.0;
        grid.push_back( s );
    }
    return grid;
}

// One line per (scene, stats, colour stats on/off, display stats on/off): every slider the preset carries.
inline QByteArray dump( const PresetFn &presetFor )
{
    using namespace lookassist;
    const std::vector<LookAssistStats> grid = statsGrid();
    LookAssistStats display;
    display.median = 50.0;
    display.p99 = 120.0;
    QByteArray out;
    int index = 0;
    for( const LookAssistStats &s : grid )
    {
        for( int scene = 0; scene < 4; ++scene )
            for( int useColor = 0; useColor < 2; ++useColor )
                for( int useDisplay = 0; useDisplay < 2; ++useDisplay )
                {
                    const LookAssistPreset p = presetFor( static_cast<LookAssistScene>( scene ), s,
                                                          useColor ? &s : nullptr,
                                                          useDisplay ? &display : nullptr );
                    out += QString( "%1 %2 %3 %4 | exp=%5 contrast=%6 pivot=%7 shadows=%8 highlights=%9 vibrance=%10 temp=%11 tint=%12\n" )
                               .arg( index ).arg( scene ).arg( useColor ).arg( useDisplay )
                               .arg( p.exposure ).arg( p.contrast ).arg( p.pivot ).arg( p.shadows )
                               .arg( p.highlights ).arg( p.vibrance ).arg( p.temperatureDelta ).arg( p.tintDelta )
                               .toUtf8();
                }
        ++index;
    }
    return out;
}

} // namespace look_assist_flavor_grid

#endif
