#ifndef LOOK_ASSIST_ANALYSIS_PUMP_H
#define LOOK_ASSIST_ANALYSIS_PUMP_H

// LOOK-ASSIST-CLIP-OPEN-GUI-BLOCK-1: the telemetry of one Look Assist apply.

#include <QElapsedTimer>
#include <QString>
#include <QStringLiteral>

namespace look_assist_pump {

// One look_assist.apply.timing line: the time the apply spent in each segment. A daylight verdict that falls back to
// master's pass re-enters the apply, and that pass adds to the same line, so one analysis writes one line.
struct ApplyTiming
{
    double rawThumbMs = 0.0;
    double sceneMs = 0.0;
    double processedMs = 0.0;
    double meterMs = 0.0;
    double solveMs = 0.0;
    double windowLitMs = 0.0;
    double walkMs = 0.0;
    double totalMs = 0.0;
    double guiBlockMaxMs = 0.0;
    unsigned long long pumpTicks = 0;
    bool worker = false;

    QString logFields() const
    {
        return QStringLiteral( "raw_thumb_ms=%1 scene_ms=%2 processed_ms=%3 meter_ms=%4 solve_ms=%5 window_lit_ms=%6 "
                               "walk_ms=%7 total_ms=%8 gui_block_max_ms=%9 pump_ticks=%10 route=%11" )
            .arg( rawThumbMs, 0, 'f', 1 )
            .arg( sceneMs, 0, 'f', 1 )
            .arg( processedMs, 0, 'f', 1 )
            .arg( meterMs, 0, 'f', 1 )
            .arg( solveMs, 0, 'f', 1 )
            .arg( windowLitMs, 0, 'f', 1 )
            .arg( walkMs, 0, 'f', 1 )
            .arg( totalMs, 0, 'f', 1 )
            .arg( guiBlockMaxMs, 0, 'f', 1 )
            .arg( pumpTicks )
            .arg( worker ? QStringLiteral( "worker" ) : QStringLiteral( "inline" ) );
    }
};

// Adds the scope's elapsed time to one segment of the line.
class SegmentClock
{
public:
    explicit SegmentClock( double *segmentMs ) : m_segmentMs( segmentMs ) { m_clock.start(); }
    ~SegmentClock() { *m_segmentMs += m_clock.nsecsElapsed() / 1000000.0; }
    SegmentClock( const SegmentClock & ) = delete;
    SegmentClock &operator=( const SegmentClock & ) = delete;

private:
    double *m_segmentMs;
    QElapsedTimer m_clock;
};

} // namespace look_assist_pump

#endif // LOOK_ASSIST_ANALYSIS_PUMP_H
