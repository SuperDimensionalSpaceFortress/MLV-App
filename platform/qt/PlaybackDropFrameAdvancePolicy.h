/*!
 * \file PlaybackDropFrameAdvancePolicy.h
 * \brief Pure arithmetic: how much source-frame distance a drop-frame-mode
 *        playback step should credit to
 *        MainWindow::m_playbackTimelineSourceFramesOffered -- extracted from
 *        MainWindow::playbackHandling() so it can be unit-tested without the
 *        GUI.
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 7 (astra major, "Non-looping EOF
 * overshoot inflates the denominator"): playbackHandling() computes the raw,
 * uncapped source-frame distance a step advances
 * (getFramerate() * timeDiff / 1000.0) and used to add that FULL raw amount
 * to the offered accumulator even when the step's own clamp then pulls the
 * position back to the clip's last frame because looping is off. astra's
 * repro: position 98, last valid frame 99, looping disabled, 30 FPS, a
 * 100 ms tick -- the raw delta is 3.0 source frames, but only ONE source
 * frame (98 -> 99) actually exists to advance across; frames 100 and 101
 * never existed. Crediting the raw 3.0 to offered let two nonexistent
 * frames become confidently reported source-frame loss purely because the
 * clip ended mid-tick. This policy caps the credited amount at the distance
 * actually travelled to the clamped end position; the looping case is
 * unaffected (a lap wrap is real, unbounded, source-frame distance -- see
 * playbackHandling()'s own wrap-crediting comment).
 */

#ifndef PLAYBACKDROPFRAMEADVANCEPOLICY_H
#define PLAYBACKDROPFRAMEADVANCEPOLICY_H

class PlaybackDropFrameAdvancePolicy
{
public:
    /*! \param currentPosition MainWindow::m_newPosDropMode BEFORE this
     *         step's raw advance is added.
     *  \param rawAdvance getFramerate() * timeDiff / 1000.0 -- the
     *         uncapped source-frame distance this tick would travel.
     *  \param loopEnabled ui->actionLoop->isChecked() -- when true, the raw
     *         advance is returned unchanged; a loop wrap is a real distance
     *         travelled, not an EOF overshoot.
     *  \param lastFrameIndex ui->spinBoxCutOut->value() - 1 -- the last
     *         valid 0-based frame index (0 <= frame < cutOut), i.e. the
     *         position the non-looping clamp pulls back to.
     *  \return the source-frame distance to credit to
     *          MainWindow::m_playbackTimelineSourceFramesOffered for this
     *          step: rawAdvance unchanged unless looping is off AND the raw
     *          advance would carry currentPosition to or past
     *          lastFrameIndex, in which case it is capped at the distance
     *          from currentPosition to lastFrameIndex (never negative).
     */
    static double offeredAdvance( double currentPosition,
                                  double rawAdvance,
                                  bool loopEnabled,
                                  double lastFrameIndex )
    {
        if( loopEnabled ) return rawAdvance;
        const double rawNewPosition = currentPosition + rawAdvance;
        if( rawNewPosition < lastFrameIndex ) return rawAdvance;
        const double cappedAdvance = lastFrameIndex - currentPosition;
        return cappedAdvance > 0.0 ? cappedAdvance : 0.0;
    }
};

#endif // PLAYBACKDROPFRAMEADVANCEPOLICY_H
