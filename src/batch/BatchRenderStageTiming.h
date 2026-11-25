#ifndef MLVAPP_BATCH_RENDER_STAGE_TIMING_H
#define MLVAPP_BATCH_RENDER_STAGE_TIMING_H

/* EXPORT-RENDER-PIPE-TIMING-1: the rendered (ProRes) batch export's stage notes, under the EXISTING
 * MLVAPP_STAGE_TIMING sink (src/debug/StageTiming.h). mlv_stage_timing_note() writes the thread-local
 * snapshot before it checks the enabled flag, so this helper reads the flag ONCE (BatchRunner declares
 * one before the frame loop) and, when timing is off, reads no clock and notes nothing: an untimed export
 * pays one cached flag test per stage. Header-only so tests/pipeline/test_batch_render_stage_timing.cpp
 * exercises the same code BatchRunner.cpp runs. */

#include <stdint.h>

#include "../debug/StageTiming.h"

struct BatchRenderStageTiming
{
    const bool on;

    BatchRenderStageTiming() : on( mlv_stage_timing_enabled() != 0 ) {}

    /* The clock, read only when timing is on (0.0 otherwise, never passed to a note). */
    double start() const
    {
        return on ? mlv_stage_timing_now() : 0.0;
    }

    void note( const char *stage, uint64_t frameIndex, double startTime ) const
    {
        if( !on ) return;
        mlv_stage_timing_note( stage, frameIndex, startTime );
    }
};

#endif
