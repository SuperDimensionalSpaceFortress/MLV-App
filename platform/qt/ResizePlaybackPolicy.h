#ifndef RESIZEPLAYBACKPOLICY_H
#define RESIZEPLAYBACKPOLICY_H

// PLAYER-FULLSCREEN-KEEPS-PLAYING-1: what MainWindow::resizeEvent does with one resize (a fullscreen toggle, a
// maximize/restore, a window drag). It used to uncheck Play on every resize, so entering or leaving full screen ended
// playback. A resize during Play now does not stop Play at all: every playback drawFrame() sizes its scene from the
// current geometry (computeDisplaySceneGeometry), and finishPresentedFrame re-anchors the gradient when the landed
// scene changes. Nothing is stopped, so nothing is resumed: a resume would be a programmatic Play, which the Play gate
// (tools/repo_hygiene/app_play_scan.py, PLAYBACK-CLIP-LENGTH-ENFORCE-2) forbids outside its pinned helpers.
//
// Pure decision, no Qt GUI types.

namespace resize_playback_policy
{

enum class Action
{
    AcceptOnly,          // a clip open is in progress: accept the event, nothing else
    HoldForLookAssist,   // a Look Assist analysis is open: hold the resize, the held replay re-decides at landing
    KeepPlaying,         // Play is on: keep playing; the next playback frame sizes itself from the new geometry
    RedrawStopped        // Play is off: redraw the still frame at the new size
};

// Precedence mirrors master's resizeEvent: an open in progress, then a Look Assist session (the held replay
// re-decides at landing), then Play.
inline Action decide( bool openingInProgress, bool lookAssistSessionOpen, bool playChecked )
{
    if( openingInProgress ) return Action::AcceptOnly;
    if( lookAssistSessionOpen ) return Action::HoldForLookAssist;
    if( playChecked ) return Action::KeepPlaying;
    return Action::RedrawStopped;
}

inline const char *actionName( Action a )
{
    switch( a )
    {
    case Action::AcceptOnly: return "accept_only";
    case Action::HoldForLookAssist: return "hold_for_look_assist";
    case Action::KeepPlaying: return "keep_playing";
    case Action::RedrawStopped: return "redraw_stopped";
    }
    return "unknown";
}

} // namespace resize_playback_policy

#endif // RESIZEPLAYBACKPOLICY_H
