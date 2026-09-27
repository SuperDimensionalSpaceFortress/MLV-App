/*!
 * \file AsyncH2dCounterContract.h
 * \brief Counter record contract for the async host-to-device (H2D) GPU
 *        upload path, and a pure validator over it. PLAY-C2-SUBMIT-2-ACCEPT
 *        consumes this contract to accept a submitted counter record set.
 */

#ifndef ASYNCH2DCOUNTERCONTRACT_H
#define ASYNCH2DCOUNTERCONTRACT_H

#include <cstdint>
#include <set>
#include <string>
#include <vector>

namespace async_h2d_counter_contract {

/*! \brief One async H2D upload counter sample for a single frame id.
 *
 * frameId is the RAW explicit per-frame id as passed by the playback path
 * (open PR #72's `uint64_t frame_id`), NOT the derived `frame_id + 1`
 * frame_token — callers must not conflate the two.
 *
 * buildIdentity MUST be the lowercase hex SHA-256 of the emitting binary,
 * the same value the merged provenance model (PR #67,
 * tools/repo_hygiene/gpu_job_result_provenance.py) records as `dllSha256`.
 * Every record in a submitted set must match the caller's expected build
 * identity, so a record from a stale or mixed build is never silently
 * accepted alongside current-build records.
 */
struct AsyncH2dCounterRecord
{
    std::uint64_t frameId = 0;
    std::uint64_t fires = 0;
    std::uint64_t bytes = 0;
    std::string buildIdentity;
};

enum class AsyncH2dCounterVerdict
{
    Valid,
    Empty,
    DuplicateFrameId,
    FiresZeroWithBytesPositive,
    MalformedBuildIdentity,
    BuildIdentityMismatch,
};

/*! \brief Pure validator over an async H2D counter record set.
 *
 * Every rule is checked in a fixed order so a caller gets the first
 * violation deterministically.
 */
class AsyncH2dCounterValidator
{
public:
    static AsyncH2dCounterVerdict evaluate(
        const std::vector<AsyncH2dCounterRecord> &records,
        const std::string &expectedBuildIdentity )
    {
        if( records.empty() ) return AsyncH2dCounterVerdict::Empty;

        std::set<std::uint64_t> seenFrameIds;
        for( const AsyncH2dCounterRecord &record : records )
        {
            if( !seenFrameIds.insert( record.frameId ).second )
                return AsyncH2dCounterVerdict::DuplicateFrameId;
            if( record.fires == 0 && record.bytes > 0 )
                return AsyncH2dCounterVerdict::FiresZeroWithBytesPositive;
            if( !isWellFormedBuildIdentity( record.buildIdentity ) )
                return AsyncH2dCounterVerdict::MalformedBuildIdentity;
            if( record.buildIdentity != expectedBuildIdentity )
                return AsyncH2dCounterVerdict::BuildIdentityMismatch;
        }
        return AsyncH2dCounterVerdict::Valid;
    }

    static bool accepts( const std::vector<AsyncH2dCounterRecord> &records,
                          const std::string &expectedBuildIdentity )
    {
        return evaluate( records, expectedBuildIdentity ) == AsyncH2dCounterVerdict::Valid;
    }

private:
    /*! \brief Shape check only: exactly 64 lowercase hex chars (a SHA-256
     * digest), never a semantic check that the hash was actually computed
     * over the emitting binary.
     */
    static bool isWellFormedBuildIdentity( const std::string &buildIdentity )
    {
        if( buildIdentity.size() != 64 ) return false;
        for( char c : buildIdentity )
        {
            const bool isLowerHexDigit = ( c >= '0' && c <= '9' ) || ( c >= 'a' && c <= 'f' );
            if( !isLowerHexDigit ) return false;
        }
        return true;
    }
};

/*! \brief Injectable source of async H2D counter records.
 *
 * The real async H2D path binds an implementation that reads live CUDA
 * counters; tests bind a FAKE that returns a caller-constructed record set,
 * so AsyncH2dCounterValidator is exercised without any GPU/CUDA dependency.
 */
class AsyncH2dCounterBackend
{
public:
    virtual ~AsyncH2dCounterBackend() = default;
    virtual std::vector<AsyncH2dCounterRecord> collectRecords() const = 0;
};

} // namespace async_h2d_counter_contract

#endif // ASYNCH2DCOUNTERCONTRACT_H
