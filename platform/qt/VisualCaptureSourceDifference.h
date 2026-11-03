#ifndef VISUALCAPTURESOURCEDIFFERENCE_H
#define VISUALCAPTURESOURCEDIFFERENCE_H

/* PLAYBACK-SOURCE-FRAME-IDENTITY-1: the "source_difference" verdict the --visual-capture-dir
 * writer puts in visual.json, from the SOURCE-frame identities (getMlvFrameSourceHash64) of the
 * presented play frames. Display-side hashes are no substitute: a frozen display freezes them.
 *
 * Runner contract (tools/profiling/dual-venue/DualVenueRunner.psm1, Get-DvVisualVerdict, PR #474;
 * card PLAYBACK-SOURCE-DIFFERENCE-SCALAR-ONLY-1): the field is read as a case-sensitive scalar
 * JSON string, "differ" or "identical"; anything else, absence included, is unknown. "differ"
 * turns one picture into FROZEN_DISPLAY; "identical" WAIVES the frozen gate (a static clip), so
 * it is written only on positive evidence: >= 2 play frames, every one with an identity, on
 * >= 2 distinct display frames. A repeated display index is a stuck playback clock, not
 * evidence of a static clip. Without that evidence the field is removed, never null or empty. */

#include <QHash>
#include <QJsonObject>
#include <QJsonValue>
#include <QSet>
#include <QString>
#include <QVector>
#include <QtGlobal>

namespace visual_capture_source_difference
{

struct PlayIdentity
{
    int displayFrame = -1;
    bool valid = false;
    quint64 hash = 0;
};

enum class Verdict { Differ, Identical, Unknown };

inline Verdict decide( const QVector<PlayIdentity> &play )
{
    if( play.size() < 2 ) return Verdict::Unknown;
    QSet<int> distinctFrames;
    for( const PlayIdentity &id : play )
    {
        if( !id.valid || id.displayFrame < 0 ) return Verdict::Unknown;
        distinctFrames.insert( id.displayFrame );
    }
    if( distinctFrames.size() < 2 ) return Verdict::Unknown;
    for( const PlayIdentity &id : play )
    {
        if( id.hash != play.first().hash ) return Verdict::Differ;
    }
    return Verdict::Identical;
}

inline void apply( QJsonObject &visualJson, Verdict verdict )
{
    const QString key = QStringLiteral("source_difference");
    switch( verdict )
    {
    case Verdict::Differ:
        visualJson.insert( key, QJsonValue( QStringLiteral("differ") ) );
        break;
    case Verdict::Identical:
        visualJson.insert( key, QJsonValue( QStringLiteral("identical") ) );
        break;
    case Verdict::Unknown:
    default:
        visualJson.remove( key );
        break;
    }
}

/* Which visual.json capture kinds carry a source_id: the forced "play" captures and the unforced
 * "play_unbound" ones. Lives here, not as a lambda in MainWindow::runGuiPlaybackSmoke, because
 * test_dual_venue_evidence pins that the capture block contains no `return`. */
inline bool isPlayCaptureKind( const QString &kind )
{
    return kind == QStringLiteral("play") || kind == QStringLiteral("play_unbound");
}

/* A frame's source_id in visual.json: 16 lowercase hex digits, zero-padded. */
inline QString sourceIdHex( quint64 hash )
{
    return QStringLiteral("%1").arg( hash, 16, 16, QLatin1Char('0') ).toLower();
}

/* r7 (adversary r6 pass 3 A2): WHICH clip a capture was taken from. The capture path pumps events (the
 * tail's waits, the post-stop drain, the paused settle), and a QFileOpenEvent handled there replaces the
 * open clip before the deferred source hashing runs, so hashing "the open clip at display_frame" would
 * attach the NEW clip's identities to the OLD clip's pictures. A clip is named by the clip-lifecycle
 * generation (ClipLifecycleBarrier: bumped by every free of the open clip) plus the open object's
 * address; both must match. */
struct ClipToken
{
    quint64 generation = 0;
    const void *object = nullptr;
};

inline bool sameClip( const ClipToken &a, const ClipToken &b )
{
    return a.generation == b.generation && a.object == b.object;
}

template <class Barrier, class Object>
inline ClipToken clipToken( const Barrier &barrier, Object *const &openClip )
{
    ClipToken token;
    token.generation = static_cast<quint64>( barrier.generation() );
    token.object = static_cast<const void *>( openClip );
    return token;
}

/* One saved capture as the source binding sees it. */
struct CaptureClip
{
    QString kind;
    int displayFrame = -1;
    ClipToken clip;   // the clip open when the capture was taken
};

/* Per capture (same order as the input): whether it has a source id, and the id. playIdentities holds
 * the play captures only, in order, for decide(). */
struct BoundSourceIds
{
    QVector<bool> valid;
    QVector<quint64> hash;
    QVector<PlayIdentity> playIdentities;
    int clipChangedCaptures = 0;   // play captures refused because their clip is not the capture's clip
};

/* Attach a source identity to each play capture ONLY from the clip the capture run started on, while
 * that clip is still open: a capture taken from another clip, or one hashed after the open clip changed,
 * gets NO id (fail closed: source_difference becomes unknown), never another clip's. The open clip is
 * re-read before AND after each hash (hashFrame must not pump events, but a hash finished across a clip
 * change is discarded rather than trusted). hashFrame( int displayFrame, quint64 *hash, bool *ok ) reads
 * the currently open clip; a frame is hashed once. */
template <class Barrier, class Object, class HashFrame>
inline BoundSourceIds bindSourceIds( const QVector<CaptureClip> &captures, const ClipToken &captureClip,
                                     const Barrier &barrier, Object *const &openClip, HashFrame hashFrame )
{
    BoundSourceIds out;
    QHash<int, quint64> known;
    QSet<int> failed;
    for( const CaptureClip &capture : captures )
    {
        bool valid = false;
        quint64 hash = 0;
        if( isPlayCaptureKind( capture.kind ) )
        {
            const bool fromCaptureClip = sameClip( capture.clip, captureClip );
            if( !fromCaptureClip ) ++out.clipChangedCaptures;
            if( fromCaptureClip && capture.displayFrame >= 0 && !failed.contains( capture.displayFrame )
             && sameClip( clipToken( barrier, openClip ), captureClip ) )
            {
                if( !known.contains( capture.displayFrame ) )
                {
                    quint64 frameHash = 0;
                    bool ok = false;
                    hashFrame( capture.displayFrame, &frameHash, &ok );
                    if( !sameClip( clipToken( barrier, openClip ), captureClip ) )
                        ++out.clipChangedCaptures;   // hashed across a clip change: not this capture's clip
                    else if( ok )
                        known.insert( capture.displayFrame, frameHash );
                    else
                        failed.insert( capture.displayFrame );
                }
                const auto at = known.constFind( capture.displayFrame );
                if( at != known.constEnd() && sameClip( clipToken( barrier, openClip ), captureClip ) )
                {
                    valid = true;
                    hash = at.value();
                }
            }
            else if( fromCaptureClip && capture.displayFrame >= 0 && !sameClip( clipToken( barrier, openClip ), captureClip ) )
            {
                ++out.clipChangedCaptures;   // the open clip is no longer the capture's clip
            }
            PlayIdentity identity;
            identity.displayFrame = capture.displayFrame;
            identity.valid = valid;
            identity.hash = hash;
            out.playIdentities.append( identity );
        }
        out.valid.append( valid );
        out.hash.append( hash );
    }
    return out;
}

} // namespace visual_capture_source_difference

#endif // VISUALCAPTURESOURCEDIFFERENCE_H
