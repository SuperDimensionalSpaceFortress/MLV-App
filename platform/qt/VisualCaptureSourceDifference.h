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

/* A frame's source_id in visual.json: 16 lowercase hex digits, zero-padded. */
inline QString sourceIdHex( quint64 hash )
{
    return QStringLiteral("%1").arg( hash, 16, 16, QLatin1Char('0') ).toLower();
}

} // namespace visual_capture_source_difference

#endif // VISUALCAPTURESOURCEDIFFERENCE_H
