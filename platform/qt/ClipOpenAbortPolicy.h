// CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1 r3 (key3 INV-1/INV-2, Dot B2/B3): what a caller of openMlv /
// openMlvForPreview does when the open is refused because a playback-prep task still borrows the clip.
// The refusal codes sit outside enum mlv_err, so a caller never mistakes them for a corrupt file.
#ifndef CLIPOPENABORTPOLICY_H
#define CLIPOPENABORTPOLICY_H

// Refused at the quiescence guard, before any side effect: the retained clip is still loaded, unchanged.
constexpr int kClipOpenAbortedPrepBusy = 1000;
// Refused by the backstop after the retained clip was freed: an empty object is loaded instead.
constexpr int kClipOpenAbortedPrepBusyAfterFree = 1001;

// Export retries a refused open this many times, this far apart, before it reports and skips the job.
constexpr int kExportOpenAbortMaxRetries = 5;
constexpr int kExportOpenAbortRetryDelayMs = 500;
// A reopen (rename, end of export) refused by a busy prep task is retried once, this much later.
constexpr int kClipReopenAbortRetryDelayMs = 500;

inline bool clipOpenAbortedForPrep( int openResult )
{
    return openResult == kClipOpenAbortedPrepBusy || openResult == kClipOpenAbortedPrepBusyAfterFree;
}

inline bool clipOpenAbortKeptRetainedClip( int openResult )
{
    return openResult == kClipOpenAbortedPrepBusy;
}

// r4: openMlv detects a corrupt file and the prep-busy guard before it frees the retained clip, so only the
// after-free refusal leaves a different (empty) object loaded.
inline bool clipOpenFailureKeptRetainedClip( int openResult )
{
    return openResult != 0 && openResult != kClipOpenAbortedPrepBusyAfterFree;
}

// r4 (adversary A4): a deferred reopen runs only against the clip state it was scheduled for (no clip opened
// since) and never while something holds the loaded clip (a batch export, Export Current Frame, any modal
// dialog): a disabled GUI does not stop a timer. A still-current retry is postponed while the clip is held.
constexpr int kClipReopenAbortMaxDeferrals = 120; // x kClipReopenAbortRetryDelayMs = one minute

enum class DeferredClipReopen
{
    Run,
    Postpone,
    Drop
};

inline DeferredClipReopen deferredClipReopenDecision( unsigned long long scheduledGeneration,
                                                      unsigned long long currentGeneration, bool clipHeld,
                                                      int row, int activeRow, int clipCount, int deferrals )
{
    if( scheduledGeneration != currentGeneration ) return DeferredClipReopen::Drop;
    if( row < 0 || row >= clipCount || row != activeRow ) return DeferredClipReopen::Drop;
    if( clipHeld ) return deferrals < kClipReopenAbortMaxDeferrals ? DeferredClipReopen::Postpone : DeferredClipReopen::Drop;
    return DeferredClipReopen::Run;
}

// r5 (Dot 29eac4d5 A1 = adversary 008a3a27 A1): a write that goes straight into the active receipt, around
// setReceipt (Play's cut-range repair, drawFrameReady's Dual ISO autocorrection, the deferred Look Assist apply,
// the flavor selector), runs only while that receipt owns the loaded clip: the sliders are attached and the clip
// that is open is the active row's. After a failed final export reopen the job's clip stays loaded under the
// restored active row, so every such write is a no-op until a clip opens.
inline bool activeReceiptOwnsLoadedClip( bool slidersDetached, bool clipLoaded, bool hasActiveReceipt,
                                         bool loadedIsActiveClip )
{
    return !slidersDetached && clipLoaded && hasActiveReceipt && loadedIsActiveClip;
}

// Play's cut-range repair clamps the range it plays (the cut spin boxes) on any loaded clip; it saves that
// range into the receipt only when the receipt owns the clip.
inline bool playbackCutRangeRepairWritesReceipt( bool rangeChanged, bool receiptOwnsLoadedClip )
{
    return rangeChanged && receiptOwnsLoadedClip;
}

// drawFrameReady: the Dual ISO autocorrection flag and a detected pattern go into the receipt only when it owns
// the clip the frame came from; the widgets still show the retained clip's frame.
struct DualIsoFrameReceiptWrites
{
    bool autoCorrected = false;
    bool pattern = false;
};

inline DualIsoFrameReceiptWrites dualIsoFrameReceiptWrites( bool receiptOwnsLoadedClip, int dualIsoToolMode,
                                                            int readyFramePattern )
{
    DualIsoFrameReceiptWrites writes;
    if( !receiptOwnsLoadedClip || dualIsoToolMode <= 0 ) return writes;
    writes.autoCorrected = true;
    writes.pattern = readyFramePattern < 0;
    return writes;
}

// r5 (adversary 4b41aa22 A1): a batch import (drop, Open dialog, FileOpen event, DNG folder, FCPXML) shows the
// session's last row afterwards unless every import it ran was refused by a busy prep task. Such a refusal removed
// the new row and restored the clip the sliders describe; showing another row would free that clip.
struct ImportBatchOutcome
{
    bool refused = false;
    bool other = false;
    void note( bool importNotRefusedForPrep )
    {
        if( importNotRefusedForPrep ) other = true;
        else refused = true;
    }
    bool showLastRow( void ) const { return !refused || other; }
};

// showFileInEditor( row ) after openMlv( row ) failed. The active row moves back to the clip the sliders
// still describe before anything saves a receipt, so a later setReceipt( ACTIVE_RECEIPT ) never writes
// one clip's sliders into another clip's receipt. The old clip is reopened only when it is no longer loaded.
struct ShowFileOpenFailurePlan
{
    bool restoreOldActive = false;
    bool reselectOld = false;
    bool reopenOld = false;
    // r4 (adversary A1): the sliders describe the active row only when that row is the old one again and its
    // clip is still loaded. Otherwise (the delete path pre-activates the row that failed, or the old clip was
    // freed) they describe no receipt until a clip opens, so nothing saves or sanitizes a receipt from them.
    bool detachSliders = false;
    // A prep refusal that left the sliders detached is retried once later (no reopen of the old clip runs now).
    bool retryReopenLater = false;
};

inline ShowFileOpenFailurePlan showFileOpenFailurePlan( int openResult, int row, int oldActive,
                                                        int selectedCount, int clipCount, bool oldNeverLoaded,
                                                        bool oldClipStillLoaded = true )
{
    ShowFileOpenFailurePlan plan;
    const bool oldIsOther = oldActive >= 0 && oldActive < clipCount && oldActive != row;
    plan.restoreOldActive = oldIsOther;
    plan.reselectOld = oldIsOther && selectedCount <= 1;
    plan.reopenOld = plan.reselectOld && !oldNeverLoaded && !clipOpenAbortKeptRetainedClip( openResult );
    // r6 (adversary 7354a575 A1): an open that ran inside the failed open's error dialog (a FileOpen import)
    // loaded another clip, so the sliders describe that clip and not the old row, whatever openResult says.
    // r6 (adversary e11815a5 A2): reopening the active row itself (oldActive == row) that fails but keeps its
    // clip loaded leaves the sliders describing that row; the delete path's row is not the loaded clip.
    const bool oldIsRowItself = oldActive >= 0 && oldActive < clipCount && oldActive == row;
    plan.detachSliders = !( ( plan.restoreOldActive || oldIsRowItself ) && !oldNeverLoaded
                            && clipOpenFailureKeptRetainedClip( openResult ) && oldClipStillLoaded );
    plan.retryReopenLater = plan.detachSliders && !plan.reopenOld && clipOpenAbortedForPrep( openResult );
    return plan;
}

// openSession: a clip whose open failed gets its receipt from the XML, but neither its settings nor its
// thumbnail are taken from the clip that is still loaded, and it does not become the active row.
// setSliders runs ReceiptSafety against the loaded clip and rewrites the receipt (adversary A3), so a
// failed clip never takes sliders; when no session clip opened, the last one is reopened later instead.
struct SessionClipOpenFailurePlan
{
    bool applySliders = false;
    bool takeThumbnail = false;
    bool activateClip = false;
    bool restoreLastOpened = false;
};

inline SessionClipOpenFailurePlan sessionClipOpenPlan( int openResult, int lastOpenedRow )
{
    SessionClipOpenFailurePlan plan;
    if( openResult == 0 )
    {
        plan.applySliders = true;
        plan.takeThumbnail = true;
        plan.activateClip = true;
        return plan;
    }
    plan.restoreLastOpened = lastOpenedRow >= 0;
    return plan;
}

enum class ExportOpenFailureAction
{
    SkipCorruptJob,
    RetryJob,
    ReportAndSkipJob
};

inline ExportOpenFailureAction exportOpenFailureAction( int openResult, int retriesSoFar )
{
    if( !clipOpenAbortedForPrep( openResult ) ) return ExportOpenFailureAction::SkipCorruptJob;
    if( retriesSoFar < kExportOpenAbortMaxRetries ) return ExportOpenFailureAction::RetryJob;
    return ExportOpenFailureAction::ReportAndSkipJob;
}

#endif // CLIPOPENABORTPOLICY_H
