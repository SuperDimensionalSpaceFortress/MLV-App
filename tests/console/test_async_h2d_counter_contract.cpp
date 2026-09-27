#include "../common/minitest.h"

#include "../../platform/qt/AsyncH2dCounterContract.h"

using async_h2d_counter_contract::AsyncH2dCounterBackend;
using async_h2d_counter_contract::AsyncH2dCounterRecord;
using async_h2d_counter_contract::AsyncH2dCounterValidator;
using async_h2d_counter_contract::AsyncH2dCounterVerdict;

namespace {

const char * const kBuildIdentity =
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const char * const kStaleBuildIdentity =
    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

// FAKE backend: production binds an implementation that reads live CUDA
// counters; this test binds a caller-constructed record set directly, so
// the validator is exercised without any GPU/CUDA dependency.
class FakeAsyncH2dCounterBackend : public AsyncH2dCounterBackend
{
public:
    explicit FakeAsyncH2dCounterBackend( std::vector<AsyncH2dCounterRecord> records )
        : m_records( std::move( records ) )
    {
    }

    std::vector<AsyncH2dCounterRecord> collectRecords() const override
    {
        return m_records;
    }

private:
    std::vector<AsyncH2dCounterRecord> m_records;
};

std::vector<AsyncH2dCounterRecord> wellFormedRecords()
{
    return {
        AsyncH2dCounterRecord{ 0, 1, 4096, kBuildIdentity },
        AsyncH2dCounterRecord{ 1, 1, 4096, kBuildIdentity },
        AsyncH2dCounterRecord{ 2, 2, 8192, kBuildIdentity },
    };
}

} // namespace

TEST(AsyncH2dCounterContract, AcceptsWellFormedFakeBackendRecordSet)
{
    const FakeAsyncH2dCounterBackend backend( wellFormedRecords() );
    const std::vector<AsyncH2dCounterRecord> records = backend.collectRecords();

    ASSERT_TRUE( AsyncH2dCounterValidator::accepts( records, kBuildIdentity ) );
    ASSERT_TRUE( AsyncH2dCounterValidator::evaluate( records, kBuildIdentity )
                 == AsyncH2dCounterVerdict::Valid );
}

TEST(AsyncH2dCounterContract, RejectsFiresZeroWithBytesPositive)
{
    std::vector<AsyncH2dCounterRecord> records = wellFormedRecords();
    records[1].fires = 0; // bytes were transferred with no recorded fire

    const FakeAsyncH2dCounterBackend backend( records );

    ASSERT_FALSE( AsyncH2dCounterValidator::accepts( backend.collectRecords(), kBuildIdentity ) );
    ASSERT_TRUE( AsyncH2dCounterValidator::evaluate( backend.collectRecords(), kBuildIdentity )
                 == AsyncH2dCounterVerdict::FiresZeroWithBytesPositive );
}

TEST(AsyncH2dCounterContract, RejectsBuildIdentityMismatch)
{
    std::vector<AsyncH2dCounterRecord> records = wellFormedRecords();
    records[2].buildIdentity = kStaleBuildIdentity;

    const FakeAsyncH2dCounterBackend backend( records );

    ASSERT_FALSE( AsyncH2dCounterValidator::accepts( backend.collectRecords(), kBuildIdentity ) );
    ASSERT_TRUE( AsyncH2dCounterValidator::evaluate( backend.collectRecords(), kBuildIdentity )
                 == AsyncH2dCounterVerdict::BuildIdentityMismatch );
}

TEST(AsyncH2dCounterContract, RejectsMalformedBuildIdentity)
{
    std::vector<AsyncH2dCounterRecord> records = wellFormedRecords();
    records[0].buildIdentity = "not-a-sha256-hex-digest"; // wrong shape entirely

    const FakeAsyncH2dCounterBackend backend( records );

    ASSERT_FALSE( AsyncH2dCounterValidator::accepts( backend.collectRecords(), kBuildIdentity ) );
    ASSERT_TRUE( AsyncH2dCounterValidator::evaluate( backend.collectRecords(), kBuildIdentity )
                 == AsyncH2dCounterVerdict::MalformedBuildIdentity );
}

TEST(AsyncH2dCounterContract, EmptyRecordSetIsEmpty)
{
    const FakeAsyncH2dCounterBackend backend( {} );

    ASSERT_FALSE( AsyncH2dCounterValidator::accepts( backend.collectRecords(), kBuildIdentity ) );
    ASSERT_TRUE( AsyncH2dCounterValidator::evaluate( backend.collectRecords(), kBuildIdentity )
                 == AsyncH2dCounterVerdict::Empty );
}
