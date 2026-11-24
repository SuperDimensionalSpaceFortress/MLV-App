/*!
 * \file PlaybackInfoLabelPolicy.h
 * \brief LOOK-LABEL-PLAYBACK-INFO-1: the playback-info lines drawn under the fullscreen look label
 *        (and, without fps, in the status-bar twin). Pure (QtCore only), so console_tests pins it;
 *        the pixels are drawn by LookLabelOverlay.h, the facts are read by MainWindow.
 *
 * Owner, 2026-11-06 (PR 459): "We need to show other useful info as well. CPU vs GPU. Scale (Full, Half,
 * Quarter). Am I missing anything?" Owner, 2026-11-16: "show other useful info such as fps, resolution, etc".
 * The label only names what is on screen; it never changes playback.
 */

#ifndef PLAYBACKINFOLABELPOLICY_H
#define PLAYBACKINFOLABELPOLICY_H

#include "AutomationSettings.h"
#include "PlaybackConformPolicy.h"
#include "PlaybackScaleLabel.h"

#include <QChar>
#include <QString>
#include <QStringList>

#include <cmath>

namespace playback_info_label
{

/*! \brief What the screen shows, read by MainWindow from state it already keeps (no new decode work). */
struct Facts
{
    bool gpuDisplay = false;          // the GPU display window is the preview surface
    QString processingBadge;          // the toolbar's processing badge ("GPU Preview", "CPU", ...)
    int requestedScale = 0;           // 1, 2, 4, 8 (0 = unknown, the scale field is omitted)
    int presentedScale = 0;
    bool presentedValid = false;      // presentedScale belongs to the requested scale's frames
    int presentedFrameScale = 0;      // INFO-2 r1 (pass 21 A5): the scale the PRESENTED frame was rendered at, independent of the live
                                      // request (presentedValid compares against it); 0 = no valid presented frame, debayerField then
                                      // falls back to presentedScale / requestedScale.
    QString debayer;                 // the chosen debayer's menu text ("" = omitted)
    bool debayerOnGpu = false;
    QString gpuDebayer;               // r3: the algorithm the GPU ran for the presented frame ("AMaZE", "Bilinear"; "" = none)
    int presentedRoute = 0;           // r12 (adversary A1): the presented frame's CPU render-path code (render_thread_phase4b_path,
                                      // video_mlv.c); 0 = full recon or unknown. 6/7 = x1 half proxy, 9/10 = x1 quarter proxy.
    int debayerScale = 0;             // r13 (adversary pass 15 A1): the scale the CPU debayer ran at, when the frame says so (a
                                      // playback-prep frame: the render's achieved size, before the prep's box reduction);
                                      // 0 = not known apart from the shown scale, which debayerField then uses.
    bool playing = false;
    double achievedFps = 0.0;         // smoothed presented rate while playing
    double clipFps = 0.0;             // the clip's own rate (<= 0 = unknown, the fps field is omitted)
    double playbackRate = 0.0;        // the conform rate (getPlaybackFramerate)
    bool dropFrame = false;
    bool dualIsoClip = false;         // the Dual-ISO field is shown only for a Dual-ISO clip
    bool dualIsoOn = false;
    int width = 0;                    // source resolution (<= 0 = omitted)
    int height = 0;
};

/*! \brief Which fields the user shows (View > Fullscreen Info). All shown by default. */
struct Toggles
{
    bool pipeline = true;
    bool scale = true;
    bool debayer = true;
    bool fps = true;                  // the rate and DF
    bool dualIso = true;
    bool resolution = true;
};

namespace Settings
{
    inline constexpr const char * kKeyShowPipeline() { return "Playback/LookLabelInfo/ShowPipeline"; }
    inline constexpr const char * kKeyShowScale() { return "Playback/LookLabelInfo/ShowScale"; }
    inline constexpr const char * kKeyShowDebayer() { return "Playback/LookLabelInfo/ShowDebayer"; }
    inline constexpr const char * kKeyShowFps() { return "Playback/LookLabelInfo/ShowFps"; }
    inline constexpr const char * kKeyShowDualIso() { return "Playback/LookLabelInfo/ShowDualIso"; }
    inline constexpr const char * kKeyShowResolution() { return "Playback/LookLabelInfo/ShowResolution"; }
    inline constexpr bool kDefaultShow() { return true; }
}

/*! \brief " . " with a middle dot, built in code so the source stays ASCII. */
inline QString separator()
{
    return QStringLiteral( " " ) + QChar( 0x00B7 ) + QStringLiteral( " " );
}

inline QString pipelineField( const Facts &facts )
{
    QString text = facts.gpuDisplay ? QStringLiteral( "GPU display" ) : QStringLiteral( "CPU display" );
    const QString badge = facts.processingBadge.trimmed();
    if( !badge.isEmpty() ) text += QStringLiteral( " " ) + badge;
    return text;
}

inline QString scaleField( const Facts &facts )
{
    if( facts.requestedScale <= 0 ) return QString();
    if( facts.presentedValid && facts.presentedScale > 0 && facts.presentedScale != facts.requestedScale )
    {
        return PlaybackScaleLabel::fractionWord( facts.requestedScale ) + QStringLiteral( "->" )
             + PlaybackScaleLabel::fractionWord( facts.presentedScale );
    }
    return PlaybackScaleLabel::fractionWord( facts.requestedScale );
}

/*! \brief r12 adversary A1: the reduced-proxy level of a presented CPU route code (video_mlv.c
 *  mlv_render_scaled_rgb16_x1_half_preview_core): 6 (half preview, full processing) and 7 (half preview, half processing) -> 1
 *  halving; 10 and 9 (the quarter variants) -> 2. Every other code (0 = full recon, 2/3/4/8 = scaled pre-recon) -> 0. */
inline int proxyRouteHalvings( int presentedRoute )
{
    if( presentedRoute == 6 || presentedRoute == 7 ) return 1;
    if( presentedRoute == 9 || presentedRoute == 10 ) return 2;
    return 0;
}

inline QString debayerField( const Facts &facts )
{
    const QString name = facts.debayer.trimmed();
    const QString gpu = facts.gpuDebayer.trimmed();
    // r3 adversary A2 (a271daee): the GPU names what it ran. LMMSE selected passes the GPU AMaZE eligibility, so a frame
    // rendered by GPU AMaZE reads "LMMSE->AMaZE (GPU)" (selected -> ran), never "LMMSE (GPU)".
    if( facts.debayerOnGpu && !gpu.isEmpty() )
    {
        if( name.isEmpty() || name.compare( gpu, Qt::CaseInsensitive ) == 0 ) return gpu + QStringLiteral( " (GPU)" );
        return name + QStringLiteral( "->" ) + gpu + QStringLiteral( " (GPU)" );
    }
    // r12 adversary A1 (ff6a22ab): the x1 half/quarter proxy debayers bilinear (debayerBasicU16) at reduced dimensions and
    // upscales, so the output scale stays Full while the selected algorithm never ran. The presented frame's route names it.
    const int proxyHalvings = facts.debayerOnGpu ? 0 : proxyRouteHalvings( facts.presentedRoute );
    if( proxyHalvings > 0 )
    {
        const QString proxy = QStringLiteral( "Bilinear" );
        const QString level = proxyHalvings >= 2 ? QStringLiteral( " (Quarter proxy)" ) : QStringLiteral( " (Half proxy)" );
        if( name.isEmpty() || name.compare( proxy, Qt::CaseInsensitive ) == 0 ) return proxy + level;
        return name + QStringLiteral( "->" ) + proxy + level;
    }
    if( name.isEmpty() ) return QString();
    if( facts.debayerOnGpu ) return name + QStringLiteral( " (GPU)" );
    // r7 (adversary pass 10 A1 on 235e3275): a CPU frame below full scale never runs the selected debayer. Every scaled CPU
    // route bins the Bayer to RGB (pl_downsample_bayer_to_rgb_2x/4x/8x) or runs bilinear on a reduced Bayer (video_mlv.c),
    // so it reads "<selected>->Scaled" (selected -> ran), never "AMaZE". The scale is the one the scale field names as shown.
    // INFO-2 r1 (adversary pass 21 A5 on deebf5bb): the retained frame's OWN scale decides, never the live request: a paused
    // scale change makes presentedValid false (the live request no longer matches the frame's), and the label then read the new
    // request ("LMMSE->Scaled") over a frame that was debayered full size. The legacy shown scale is the fallback only.
    const int shownScale = facts.presentedFrameScale > 0 ? facts.presentedFrameScale
        : ( ( facts.presentedValid && facts.presentedScale > 0 ) ? facts.presentedScale : facts.requestedScale );
    // r13 (adversary pass 15 A1 on 189dd4f9): a playback-prep frame may debayer full size with the selected algorithm and be
    // box-reduced afterwards (the recon-reuse refusal), so the frame's own debayer scale decides, not the scale it is shown at.
    const int debayerRanScale = facts.debayerScale > 0 ? facts.debayerScale : shownScale;
    if( debayerRanScale > 1 ) return name + QStringLiteral( "->Scaled" );
    return name;
}

inline bool clipFpsKnown( const Facts &facts )
{
    return std::isfinite( facts.clipFps ) && facts.clipFps > 0.0;
}

/*! \brief Playing: "<achieved>/<clip> fps", plus " (<clip>-><rate>)" while conformed below the clip rate.
 *  Stopped: "<clip> fps". An achieved rate not yet measured (the first ticks of a play) reads "--". */
inline QString fpsField( const Facts &facts )
{
    if( !clipFpsKnown( facts ) ) return QString();
    const QString clip = playback_conform::formatRateForStatus( facts.clipFps );
    if( !facts.playing ) return clip + QStringLiteral( " fps" );
    const QString achieved = ( std::isfinite( facts.achievedFps ) && facts.achievedFps > 0.0 )
        ? QString::number( facts.achievedFps, 'f', 1 ) : QStringLiteral( "--" );
    QString text = achieved + QStringLiteral( "/" ) + clip + QStringLiteral( " fps" );
    if( std::isfinite( facts.playbackRate ) && facts.playbackRate > 0.0 && facts.playbackRate + 0.005 < facts.clipFps )
    {
        text += QStringLiteral( " (" ) + clip + QStringLiteral( "->" )
              + playback_conform::formatRateForStatus( facts.playbackRate ) + QStringLiteral( ")" );
    }
    return text;
}

inline QString dropFrameField( const Facts &facts )
{
    return facts.dropFrame ? QStringLiteral( "DF on" ) : QStringLiteral( "DF off" );
}

inline QString dualIsoField( const Facts &facts )
{
    if( !facts.dualIsoClip ) return QString();
    return facts.dualIsoOn ? QStringLiteral( "Dual-ISO on" ) : QStringLiteral( "Dual-ISO off" );
}

inline QString resolutionField( const Facts &facts )
{
    if( facts.width <= 0 || facts.height <= 0 ) return QString();
    return QString::number( facts.width ) + QStringLiteral( "x" ) + QString::number( facts.height );
}

/*! \brief At most two lines joined by '\n': line A = pipeline . scale . debayer; line B = fps . DF .
 *  Dual-ISO . resolution. A line with no enabled, known field is omitted; "" when nothing is shown.
 *  `includeFps` false (the status-bar twin, which sits beside the fps meter) drops the rate but keeps DF. */
inline QString compose( const Facts &facts, const Toggles &toggles, bool includeFps )
{
    const QString sep = separator();
    QStringList lineA;
    if( toggles.pipeline ) lineA << pipelineField( facts );
    if( toggles.scale ) lineA << scaleField( facts );
    if( toggles.debayer ) lineA << debayerField( facts );
    lineA.removeAll( QString() );

    QStringList lineB;
    if( toggles.fps && includeFps ) lineB << fpsField( facts );
    if( toggles.fps ) lineB << dropFrameField( facts );
    if( toggles.dualIso ) lineB << dualIsoField( facts );
    if( toggles.resolution ) lineB << resolutionField( facts );
    lineB.removeAll( QString() );
    // r1 adversary A5: a field never adds a line of its own (a newline inside one reads as a space). r2 A2: a CR too.
    for( QString &field : lineA ) { field.replace( QLatin1Char( '\r' ), QLatin1Char( ' ' ) ); field.replace( QLatin1Char( '\n' ), QLatin1Char( ' ' ) ); }
    for( QString &field : lineB ) { field.replace( QLatin1Char( '\r' ), QLatin1Char( ' ' ) ); field.replace( QLatin1Char( '\n' ), QLatin1Char( ' ' ) ); }

    QStringList lines;
    if( !lineA.isEmpty() ) lines << lineA.join( sep );
    if( !lineB.isEmpty() ) lines << lineB.join( sep );
    return lines.join( QLatin1Char( '\n' ) );
}

/*! \brief r2 adversary A1: is a deferred (playing) relabel due? `dirty` = an info input changed since the last relabel;
 *  `clockValid` / `elapsedMs` = a MONOTONIC clock (QElapsedTimer) restarted by every relabel, never the wall-clock QTime the
 *  fps meter reads (it wraps at midnight), and never an fps measurement (a 1 fps conform resets the meter). <= 4 Hz. */
constexpr qint64 kDeferredRelabelMs = 250;
inline bool deferredRelabelDue( bool dirty, bool clockValid, qint64 elapsedMs )
{
    return dirty && ( !clockValid || elapsedMs >= kDeferredRelabelMs );
}

/*! \brief r2 pass-2 A1: must a relabel call wait? While playing, a call within kDeferredRelabelMs of the last relabel (same
 *  monotonic clock) only marks the info dirty; the per-tick flush relabels once deferredRelabelDue. Never while stopped. */
inline bool playingRelabelDeferred( bool playing, bool clockValid, qint64 elapsedMs )
{
    return playing && clockValid && elapsedMs < kDeferredRelabelMs;
}

/*! \brief INFO-2 r2 (adversary pass 1 A7, pass 2 A1): while playing, a relabel normally only STORES its text for the next frame's
 *  paint (the label adds no paint of its own to playback). The stored text is OWED a paint: when no frame has been presented
 *  within kLabelPaintOwedMs of the store (the first frame of a Play that takes 900 ms, or any stall, whatever came before: a paused
 *  scrub, an earlier draw or none), no frame's paint is coming, so the owed paint is made. A frame presented since the store
 *  carries the text with its own paint and clears the debt. `owed` = a store is waiting (monotonic QElapsedTimer armed),
 *  `framePresentedSinceStore` = the presented-frame count moved since it was armed, `msSinceStore` = that timer's age. One frame
 *  period at 24 fps is 42 ms, so a steady Play never pays it.
 *  INFO-2 r4 (adversary r3 A3): the deadline is relative to the EXPECTED frame period (`expectedFrameMs`, from
 *  labelPaintExpectedFrameMs): kLabelPaintOwedMs when the period is unknown (<= 0), else max(kLabelPaintOwedMs, period + period/2),
 *  so a steady 10 fps Play (a frame every 100 ms) pays nothing while a stall still pays at 150 ms.
 *  INFO-2 r5 (adversary r4 A3, r5 pass A6): the deadline is labelPaintDeadlineMs, which selects the unknown-period floor before
 *  any addition (INT64_MIN overflowed the old period + period/2) and clamps a period above kLabelPaintMaxExpectedFrameMs. */
constexpr qint64 kLabelPaintOwedMs = 50;
constexpr qint64 kLabelPaintMaxExpectedFrameMs = 1000000000;   // INFO-2 r5 (r4 A3): ~11.6 days; 1.5x of it fits a qint64
inline qint64 labelPaintDeadlineMs( qint64 expectedFrameMs )
{
    if( expectedFrameMs <= 0 ) return kLabelPaintOwedMs;
    const qint64 period = expectedFrameMs > kLabelPaintMaxExpectedFrameMs ? kLabelPaintMaxExpectedFrameMs : expectedFrameMs;
    const qint64 relative = period + period / 2;
    return relative > kLabelPaintOwedMs ? relative : kLabelPaintOwedMs;
}
inline bool labelPaintOwedDue( bool owed, bool framePresentedSinceStore, qint64 msSinceStore, qint64 expectedFrameMs )
{
    return owed && !framePresentedSinceStore && msSinceStore >= labelPaintDeadlineMs( expectedFrameMs );
}

/*! \brief INFO-2 r5 (adversary r5 pass A3 on 976372d9): does the fps meter's reset enter a LABEL stall (m_playbackFramesStalled,
 *  and the stored label pushed with a repaint)? The meter resets after its fixed kFpsMeterStallMs (500 ms) without a draw, which
 *  at a 1 fps conform target is every frame: the reset at 512 ms painted the retained frame 496 ms before the paced one. The
 *  label's stall uses its own expected-period deadline (labelPaintDeadlineMs, shared with the paint debt): playing, after a draw,
 *  and the draw age at or past the deadline, negative (a wall-clock wrap) or the presentation age wrapped. At 24 fps (63 ms) and
 *  10 fps (150 ms) the meter's reset is still a label stall; below 2 fps the stored label's paint debt pays instead. */
inline bool labelFramesStallEntered( bool playing, bool hasDraw, bool presentationAgeWrapped, qint64 msSinceLastDraw,
                                     qint64 expectedFrameMs )
{
    if( !playing || !hasDraw ) return false;
    return presentationAgeWrapped || msSinceLastDraw < 0 || msSinceLastDraw >= labelPaintDeadlineMs( expectedFrameMs );
}

/*! \brief INFO-2 r7 (sol r6 LABEL-STALL on db3b7ef5): does the fps meter's reset of a measured corner rate repaint the label at
 *  once (m_playbackInfoRateWithdrawn)? Stopped, or a label stall (labelFramesStallEntered: the draw age at or past the label's
 *  expected-period deadline, or wrapped): yes, no frame follows to carry it. Playing short of that deadline: no. r5 made the stall
 *  follow the deadline but left the withdrawal on the meter's fixed 500 ms reset, so a measured rate, a 1 fps conform target and an
 *  idle tick 512 ms after the draw still queued a retained-frame paint ~1 s before the 1500 ms deadline. The withdrawn text is then
 *  only stored for the next frame's paint, and the stored label's paint debt (labelPaintOwedDue, the same deadline) pays when that
 *  frame never comes. `labelStallEntered` is labelFramesStallEntered's answer for the same reset. */
inline bool rateWithdrawalRepaintsNow( bool playing, bool labelStallEntered )
{
    return !playing || labelStallEntered;
}

/*! \brief INFO-2 r8 (sol r7 LABEL-STALL on 37257de5, the CPU viewport): the paint a corner-label change queues on the CPU viewport
 *  (GraphicsZoomView::setLookLabel). `repaintNow` is pushLookLabelOverlay's one decision, the same the GPU window's setter honours
 *  (stopped, a withdrawal that hides the label, rateWithdrawalRepaintsNow, a label stall); r7 passed it to the GPU window only, so a
 *  CPU fullscreen Play at a 1 fps target still queued a label-rect paint at the meter's 512 ms reset, ~1 s before the deadline.
 *  `framePaintPending` = a handed-over frame still waits for its paint (pending != painted serial): that paint is already queued
 *  and credits the frame as on screen (r28), so the change widens it to the whole viewport and adds no paint of its own. Otherwise a
 *  declined repaint queues NOTHING: the text is stored, the change is owed, and it rides on the next frame's hand-over (the whole
 *  viewport, as r28 requires) or on the paint debt (settleLookLabelPaintIfOwed). A repaint now covers the old and new label rects
 *  (`labelRectsEmpty`: no rect known, the whole viewport). */
enum class CpuLabelPaint { None, LabelRects, WholeViewport };
inline CpuLabelPaint cpuLabelPaint( bool repaintNow, bool framePaintPending, bool labelRectsEmpty )
{
    if( framePaintPending ) return CpuLabelPaint::WholeViewport;
    if( !repaintNow ) return CpuLabelPaint::None;
    return labelRectsEmpty ? CpuLabelPaint::WholeViewport : CpuLabelPaint::LabelRects;
}

/*! \brief INFO-2 r7 (adversary pass 10 A3 on 71d28ec9): the expected period a stored label's paint debt waits for: the larger of
 *  the period read now and the floor its store recorded. The fps meter's reset stores the withdrawn text AFTER it zeroed the EMA,
 *  so the period read at settle time fell from the measured one (a 24 fps target playing at 2.2 fps: 450 ms, deadline 675 ms) to
 *  the target's 42 ms (deadline 63 ms), and the debt painted at draw age 576 ms, before the deadline that reset had declined. The
 *  reset records the period it read before the zeroing as the debt's floor; every other store records 0 (no floor). */
inline qint64 labelPaintOwedExpectedMs( qint64 floorMs, qint64 currentMs )
{
    return floorMs > currentMs ? floorMs : currentMs;
}

/*! \brief INFO-2 r4 (adversary r3 A3): the frame period a playing label's paint debt waits for: the larger of the target period
 *  ceil(1000 / playbackFps) and the measured fps meter EMA frame time, so a 24 fps target that plays at 10 fps waits for its
 *  real 100 ms frames. Read only: the meter is not changed.
 *  INFO-2 r5 (adversary r4 A3): EVERY finite positive rate maps to its period (a 1 fps conform target is 1000 ms, 0.5 fps is
 *  2000 ms); only a non-finite or non-positive rate (or EMA) is the typed unknown 0. Each period is clamped to
 *  kLabelPaintMaxExpectedFrameMs in double before the cast, so a tiny rate or a huge EMA never overflows qint64. */
inline qint64 labelPaintPeriodMsOf( double periodMs )
{
    if( !std::isfinite( periodMs ) || periodMs <= 0.0 ) return 0;
    const double bounded = periodMs > static_cast<double>( kLabelPaintMaxExpectedFrameMs )
                         ? static_cast<double>( kLabelPaintMaxExpectedFrameMs ) : periodMs;
    return static_cast<qint64>( bounded );
}
inline qint64 labelPaintExpectedFrameMs( double playbackFps, double meterEmaFrameMs )
{
    const bool rateKnown = std::isfinite( playbackFps ) && playbackFps > 0.0;
    // 1000 / a denormal rate is +inf: a known rate whose period is past the bound, so it is the bound (never the unknown 0)
    const double targetMs = rateKnown ? std::ceil( 1000.0 / playbackFps ) : 0.0;
    const qint64 target = rateKnown ? ( std::isfinite( targetMs ) ? labelPaintPeriodMsOf( targetMs ) : kLabelPaintMaxExpectedFrameMs )
                                    : 0;
    const qint64 measured = labelPaintPeriodMsOf( std::isfinite( meterEmaFrameMs ) ? std::round( meterEmaFrameMs ) : 0.0 );
    return target > measured ? target : measured;
}

/*! \brief r3 adversary A1 (9c9941cd): does the presented frame's active scale belong to the requested scale? It compares the
 *  presented request's POLICY scale (what the setting or Auto asked for), never its effective scale: the GPU texture route clamps
 *  an x8 request to 1, and comparing that 1 with the asked 8 hid "Eighth->Full". `activeScale` must be 1, 2, 4 or 8. */
inline bool presentedScaleBelongsToRequest( bool contextValid, int contextPolicyScale, int requestedScale, int activeScale )
{
    const bool activeValid = activeScale == 1 || activeScale == 2 || activeScale == 4 || activeScale == 8;
    return activeValid && contextValid && requestedScale > 0 && contextPolicyScale == requestedScale;
}

/*! \brief INFO-2 r1 (adversary pass 21 A5 on deebf5bb): the scale the presented frame itself was rendered at, whatever scale is
 *  requested now. `activeScale` (the presented frame's shown scale) must be 1, 2, 4 or 8 and the presented request context known;
 *  0 otherwise (debayerField then keeps the legacy fallback). */
inline int presentedFrameScaleOf( bool contextValid, int activeScale )
{
    const bool activeValid = activeScale == 1 || activeScale == 2 || activeScale == 4 || activeScale == 8;
    return ( contextValid && activeValid ) ? activeScale : 0;
}

/*! \brief r12 pass-2 A1 (c58f5bd8): the scale a CPU playback-prep frame is presented at. OutputDebayered16 renders at the size the
 *  render achieved (sourceWidth / renderedWidth, full size unless it consumed a reduced recon) and the prep box-reduces it by
 *  prepReducedFactor (1 when it refuses), so the frame is shown at their product, never at the requested scale the render
 *  thread reports for it. Any input that does not give 1, 2, 4 or 8 keeps the render's own report. */
inline int presentedScaleOfCpuPrepRoute( int renderActiveScale, int sourceWidth, int renderedWidth, int prepReducedFactor )
{
    if( sourceWidth <= 0 || renderedWidth <= 0 || renderedWidth > sourceWidth || prepReducedFactor < 1 ) return renderActiveScale;
    const int achieved = static_cast<int>( std::lround( static_cast<double>( sourceWidth ) / static_cast<double>( renderedWidth ) ) );
    // r16 (adversary pass 18 A2): bound both factors before the product, so no input overflows it
    if( achieved > 8 || prepReducedFactor > 8 ) return renderActiveScale;
    const int scale = achieved * prepReducedFactor;
    return ( scale == 1 || scale == 2 || scale == 4 || scale == 8 ) ? scale : renderActiveScale;
}

/*! \brief r13 (adversary pass 15 A1 on 189dd4f9): the scale a CPU playback-prep frame was DEBAYERED at: the size the render
 *  achieved (sourceWidth / renderedWidth; 1 = the selected algorithm ran full size), never the prep's box reduction after it.
 *  0 when the inputs do not give 1, 2, 4 or 8 (debayerField then falls back to the shown scale). */
inline int debayerScaleOfCpuPrepRoute( int sourceWidth, int renderedWidth )
{
    if( sourceWidth <= 0 || renderedWidth <= 0 || renderedWidth > sourceWidth ) return 0;
    const int achieved = static_cast<int>( std::lround( static_cast<double>( sourceWidth ) / static_cast<double>( renderedWidth ) ) );
    return ( achieved == 1 || achieved == 2 || achieved == 4 || achieved == 8 ) ? achieved : 0;
}

/*! \brief r13 (adversary pass 16 A1 on deb5a42b): a CPU-debayered OutputDebayered16 frame that the CPU prep did not take (GPU
 *  preview processing presents the RGB16 as rendered) is shown, and was debayered, at the size the render achieved; the render
 *  thread reports the requested scale for it. Only for such a frame: no CPU processed stage, not presented by the viewport as a
 *  Bayer texture (gpu_playback_recon_texture_present_active), no GPU debayer. r16 (adversary pass 17 A1 on 06faad35): a GPU
 *  recon as such does not disqualify it; with texture presentation off the CUDA recon is read back and the CPU debayers it.
 *  r16 (adversary pass 18 A1 on 0ec1f394): nor does a GPU debayer; GPU AMaZE and GPU bilinear debayer the source size
 *  (RenderFrameThread), so the sizes name what is shown, and debayerField names a GPU debayer before it reads the scale.
 *  0 = not such a frame or unreadable sizes (the caller keeps the render's report). */
inline int unreducedCpuDebayerScale( bool processedStageMeasured, bool gpuTexturePresent, int sourceWidth, int renderedWidth )
{
    if( processedStageMeasured || gpuTexturePresent ) return 0;
    return debayerScaleOfCpuPrepRoute( sourceWidth, renderedWidth );
}

/*! \brief r13 (keys B1, hardening H-a): the facts of the presented frame that decide whether its CPU route tag
 *  (render_thread_phase4b_path) names what ran. MainWindow::presentedPlaybackRouteCode reads them from that frame's own
 *  telemetry; a processed-8 cache hit replays the tag the engine stored with the frame (video_mlv.c resets it to 0 on the
 *  scale-1 CPU 8-bit routes that never write it, so a cached direct-8 frame replays 0, never an older proxy code). */
struct PresentedRouteFacts
{
    bool contextValid = false;          // the presented request context is known
    int activeScale = 0;                // the presented frame's active scale (the x1 proxy cores run only at 1)
    bool gpuDebayer = false;            // GPU AMaZE or GPU bilinear debayered the frame
    bool processedStageMeasured = false; // the CPU processed stage ran for this frame
    bool direct8 = false;               // the direct-8 path rendered it (full resolution, the selected debayer)
    bool gpuRecon = false;              // the GPU playback recon produced it
    int routeTag = 0;                   // render_thread_phase4b_path of the frame
};

inline int presentedRouteOfFrame( const PresentedRouteFacts &f )
{
    if( !f.contextValid ) return 0;
    if( f.activeScale != 1 ) return 0;
    if( f.gpuDebayer ) return 0;
    if( !f.processedStageMeasured ) return 0;
    if( f.direct8 ) return 0;
    if( f.gpuRecon ) return 0;
    return f.routeTag;
}

/*! \brief One-line form for a log or receipt field: '\n' -> " / ", '"' -> '\''. */
inline QString logForm( const QString &text )
{
    QString out = text;
    // r2 adversary A2: a CR (alone or in CRLF) breaks a log line too.
    out.replace( QStringLiteral( "\r\n" ), QStringLiteral( "\n" ) );
    out.replace( QLatin1Char( '\r' ), QLatin1Char( '\n' ) );
    out.replace( QLatin1Char( '\n' ), QStringLiteral( " / " ) );
    out.replace( QLatin1Char( '"' ), QLatin1Char( '\'' ) );
    return out;
}

/*! \brief The enabled fields, comma separated ("none" when every field is off). */
inline QString fieldsCsv( const Toggles &toggles )
{
    QStringList names;
    if( toggles.pipeline ) names << QStringLiteral( "pipeline" );
    if( toggles.scale ) names << QStringLiteral( "scale" );
    if( toggles.debayer ) names << QStringLiteral( "debayer" );
    if( toggles.fps ) names << QStringLiteral( "fps" );
    if( toggles.dualIso ) names << QStringLiteral( "dual_iso" );
    if( toggles.resolution ) names << QStringLiteral( "resolution" );
    return names.isEmpty() ? QStringLiteral( "none" ) : names.join( QLatin1Char( ',' ) );
}

inline Toggles togglesFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    Toggles toggles;
    toggles.pipeline = set.value( Settings::kKeyShowPipeline(), Settings::kDefaultShow() ).toBool();
    toggles.scale = set.value( Settings::kKeyShowScale(), Settings::kDefaultShow() ).toBool();
    toggles.debayer = set.value( Settings::kKeyShowDebayer(), Settings::kDefaultShow() ).toBool();
    toggles.fps = set.value( Settings::kKeyShowFps(), Settings::kDefaultShow() ).toBool();
    toggles.dualIso = set.value( Settings::kKeyShowDualIso(), Settings::kDefaultShow() ).toBool();
    toggles.resolution = set.value( Settings::kKeyShowResolution(), Settings::kDefaultShow() ).toBool();
    return toggles;
}

/*! \brief INFO-2 r2 (adversary pass 1 A6): the settings key of one toggle (nullptr for a member that is not a toggle). */
inline const char *toggleSettingsKey( bool Toggles::*field )
{
    if( field == &Toggles::pipeline ) return Settings::kKeyShowPipeline();
    if( field == &Toggles::scale ) return Settings::kKeyShowScale();
    if( field == &Toggles::debayer ) return Settings::kKeyShowDebayer();
    if( field == &Toggles::fps ) return Settings::kKeyShowFps();
    if( field == &Toggles::dualIso ) return Settings::kKeyShowDualIso();
    if( field == &Toggles::resolution ) return Settings::kKeyShowResolution();
    return nullptr;
}

/*! \brief INFO-2 r2 (adversary pass 1 A6): persists ONLY the one toggle's own choice. The whole-struct write this replaces stored
 *  every other field from the writer's in-memory copy, so a second instance that loaded before the first one saved wrote the
 *  first's field back at its stale value and the next launch lost the choice. */
inline void toggleWriteToSettings( bool Toggles::*field, bool on )
{
    const char *key = toggleSettingsKey( field );
    if( !key ) return;
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( key, on );
}

} // namespace playback_info_label

#endif // PLAYBACKINFOLABELPOLICY_H
