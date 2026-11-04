// PLAYBACK-SOURCE-FRAME-IDENTITY-1: pins getMlvFrameSourceHash64, the source-frame identity the
// --visual-capture-dir writer puts in visual.json (source_id per play frame, and the
// source_difference verdict across them). It hashes the bytes the decoder reads for a frame
// (the encoded VIDF payload at video_index[frame].frame_offset; r5: for LJ92, the picture liblj92 decodes from it; r9: for
// JPEG2K, the picture OpenJPH decodes from its four channel spans), so it must be
// stable for one frame, differ between two frames of a moving clip, and refuse (nonzero, no
// identity) every input it cannot answer for, which makes the writer omit the field.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"
#include "ojph_decoder_test_stub.h"
#include "ojph_real_codec.h"

#include <QByteArray>
#include <QFile>
#include <QTemporaryDir>

#include <cstdint>
#include <cstdio>
#include <cstddef>
#include <cstdlib>
#include <initializer_list>
#include <string>
#include <vector>

extern "C" {
#include "../../src/mlv/liblj92/lj92.h"
}

namespace
{

bool openLargeDualIso(MlvPipelineFixture & fixture)
{
    QString error;
    return fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error);
}

// r6 (adversary A1 on a9ae667a): a 64x64 14-bit LJ92 payload of a two-level picture, encoded by liblj92 itself.
std::vector<uint8_t> encodeLj92(uint16_t left, uint16_t right)
{
    std::vector<uint16_t> picture(64 * 64);
    for (int y = 0; y < 64; ++y)
        for (int x = 0; x < 64; ++x)
            picture[y * 64 + x] = x < 32 ? left : right;
    uint8_t * encoded = nullptr;
    int encodedLength = 0;
    if (lj92_encode(picture.data(), 64, 64, 14, 64 * 64, 0, nullptr, 0, &encoded, &encodedLength) != LJ92_ERROR_NONE
        || !encoded)
        return {};
    std::vector<uint8_t> out(encoded, encoded + encodedLength);
    free(encoded);
    return out;
}

// The payload with `segment` inserted right after SOI (FF D8).
std::vector<uint8_t> withSegmentAfterSoi(const std::vector<uint8_t> & payload, const std::vector<uint8_t> & segment)
{
    std::vector<uint8_t> out(payload.begin(), payload.begin() + 2);
    out.insert(out.end(), segment.begin(), segment.end());
    out.insert(out.end(), payload.begin() + 2, payload.end());
    return out;
}

// Decodes a COPY: liblj92's parseHuff writes into the buffer it parses.
bool decodeLj92(std::vector<uint8_t> payload, std::vector<uint16_t> * picture)
{
    // r5 (adversary r5 pass 2 A2): liblj92's bit reader runs by sample count and can read past the payload, so the copy
    // carries zeroed slack (more than any 64x64 test picture's packed size), as the display decode's buffer does.
    const int length = static_cast<int>(payload.size());
    payload.resize(payload.size() + 4 + 2 * 65536, 0);
    lj92 decoder;
    int width = 0, height = 0, bitdepth = 0, components = 0;
    if (lj92_open(&decoder, payload.data(), length,
                  &width, &height, &bitdepth, &components) != LJ92_ERROR_NONE)
        return false;
    picture->assign(static_cast<size_t>(width) * height * components, 0);
    const int ret = lj92_decode(decoder, picture->data(), width * components, 0, nullptr, 0);
    lj92_close(decoder);
    return ret == LJ92_ERROR_NONE;
}

uint64_t lj92Hash(const std::vector<uint8_t> & payload, int * status)
{
    uint64_t hash = 0;
    *status = mlvLj92FrameSourceHash64(payload.data(), payload.size(), &hash);
    return hash;
}

// r5 (adversary r5 A1 on e57fd03e, opus r3 B1 on 27da457e): the LJ92 identity FOLLOWS THE DECODED PICTURE. A payload
// liblj92 refuses has no identity; two it decodes have equal identities exactly when their pictures are equal (equal
// pictures sharing one is what keeps a static clip OK; different pictures never sharing one is what keeps a frozen
// display from being waived). Returns how many of the two decoded.
int expectIdentityFollowsPicture(const std::vector<uint8_t> & a, const std::vector<uint8_t> & b)
{
    std::vector<uint16_t> aPicture;
    std::vector<uint16_t> bPicture;
    const bool aDecoded = decodeLj92(a, &aPicture);
    const bool bDecoded = decodeLj92(b, &bPicture);
    int aStatus = -1;
    int bStatus = -1;
    const uint64_t aHash = lj92Hash(a, &aStatus);
    const uint64_t bHash = lj92Hash(b, &bStatus);
    ASSERT_EQ(aDecoded, aStatus == 0);
    ASSERT_EQ(bDecoded, bStatus == 0);
    if (aDecoded && bDecoded) ASSERT_EQ(aPicture == bPicture, aHash == bHash);
    return (aDecoded ? 1 : 0) + (bDecoded ? 1 : 0);
}

} // namespace

TEST(VisualCaptureSourceHash, Lj92BytesTheDecoderSkipsAreNotHashed)
{
    const std::vector<uint8_t> plain = encodeLj92(2048, 12288);
    ASSERT_TRUE(plain.size() > 16);
    std::vector<uint16_t> plainPicture;
    ASSERT_TRUE(decodeLj92(plain, &plainPicture));
    int status = -1;
    const uint64_t plainHash = lj92Hash(plain, &status);
    ASSERT_EQ(0, status);

    // The adversary's input: a COM segment whose text differs per frame; then an APP1 segment.
    const std::vector<std::vector<uint8_t>> variants = {
        withSegmentAfterSoi(plain, {0xFF, 0xFE, 0x00, 0x03, 0x41}),
        withSegmentAfterSoi(plain, {0xFF, 0xFE, 0x00, 0x03, 0x42}),
        withSegmentAfterSoi(plain, {0xFF, 0xE1, 0x00, 0x04, 0x12, 0x34}),
    };
    for (const std::vector<uint8_t> & variant : variants)
    {
        std::vector<uint16_t> picture;
        ASSERT_TRUE(decodeLj92(variant, &picture));
        ASSERT_TRUE(picture == plainPicture); // the decoder reads the same picture...
        ASSERT_TRUE(lj92Hash(variant, &status) == plainHash); // ...so the identity is the same
        ASSERT_EQ(0, status);
    }
}

namespace
{
// The offset of the first FF <marker> in an lj92_encode header (SOI, DHT, SOF3, SOS -- all before the entropy data).
size_t markerAt(const std::vector<uint8_t> & payload, uint8_t marker)
{
    for (size_t i = 0; i + 1 < payload.size(); ++i)
        if (payload[i] == 0xFF && payload[i + 1] == marker) return i;
    return payload.size();
}
} // namespace

// r7 (adversary r6 A1 on 1fb39703): header bytes liblj92 never reads -- the DHT class/id byte, SOF3's per-component
// id / sampling / quantiser, SOS's component selector, table selector, Se and Ah/Al -- do not change the identity.
TEST(VisualCaptureSourceHash, Lj92HeaderBytesTheDecoderIgnoresAreNotHashed)
{
    const std::vector<uint8_t> plain = encodeLj92(2048, 12288);
    ASSERT_TRUE(plain.size() > 16);
    const size_t dht = markerAt(plain, 0xC4);
    const size_t sof = markerAt(plain, 0xC3);
    const size_t sos = markerAt(plain, 0xDA);
    ASSERT_TRUE(dht < sof && sof < sos && sos < plain.size());
    std::vector<uint16_t> plainPicture;
    ASSERT_TRUE(decodeLj92(plain, &plainPicture));
    int status = -1;
    const uint64_t plainHash = lj92Hash(plain, &status);
    ASSERT_EQ(0, status);
    const size_t ignored[] = { dht + 4, sof + 10, sof + 11, sof + 12, sos + 5, sos + 6, sos + 8, sos + 9 };
    for (const size_t at : ignored)
    {
        std::vector<uint8_t> variant = plain;
        variant[at] = static_cast<uint8_t>(variant[at] ^ 0x5A);
        std::vector<uint16_t> picture;
        ASSERT_TRUE(decodeLj92(variant, &picture));
        ASSERT_TRUE(picture == plainPicture);
        ASSERT_TRUE(lj92Hash(variant, &status) == plainHash);
        ASSERT_EQ(0, status);
    }
}

// r7 (adversary r6 A2 on 1fb39703): an FF D9 spelled INSIDE the SOS header (component 255, table selector 0xD9) is not the
// EOI -- the entropy data starts after the header -- so two different pictures keep different identities.
TEST(VisualCaptureSourceHash, Lj92AnEoiSpelledInTheScanHeaderNeverEndsTheHash)
{
    std::vector<uint8_t> first = encodeLj92(2048, 12288);
    std::vector<uint8_t> second = encodeLj92(3072, 12288);
    for (std::vector<uint8_t> * payload : { &first, &second })
    {
        const size_t sos = markerAt(*payload, 0xDA);
        ASSERT_TRUE(sos + 9 < payload->size());
        (*payload)[sos + 5] = 0xFF;
        (*payload)[sos + 6] = 0xD9;
    }
    std::vector<uint16_t> firstPicture;
    std::vector<uint16_t> secondPicture;
    ASSERT_TRUE(decodeLj92(first, &firstPicture));
    ASSERT_TRUE(decodeLj92(second, &secondPicture));
    ASSERT_TRUE(firstPicture != secondPicture);
    int status = -1;
    const uint64_t firstHash = lj92Hash(first, &status);
    ASSERT_EQ(0, status);
    const uint64_t secondHash = lj92Hash(second, &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(firstHash != secondHash);
}

// r8 (adversary r7 A2 on abbf1839): liblj92 reads entropy bytes by pixel count and steps over an FF D9 inside them, so
// a picture that differs only after an FF D9 in the entropy data keeps a different identity ("identical" is never false).
// r5 (adversary r5 A1): bytes after the scan that the decoder never uses no longer read "differ" either -- the identity is
// the decoded picture, so a padded payload that decodes the same picture is the same source frame.
TEST(VisualCaptureSourceHash, Lj92BytesAfterAnEoiInTheScanStillCount)
{
    // The adversary's stream: 14-bit 64x64, one 1-bit code per category (0: diff 0, 1: diff +-1), predictor 1.
    const std::vector<uint8_t> header = {
        0xFF, 0xD8,
        0xFF, 0xC4, 0x00, 0x15, 0x00,                    // DHT, length 21, class/id
        2, 0, 0, 0,  0, 0, 0, 0,  0, 0, 0, 0,  0, 0, 0, 0,  // 16 code counts
        0, 1,                                            // values
        0xFF, 0xC3, 0x00, 0x0B, 0x0E, 0x00, 0x40, 0x00, 0x40, 0x01, 0x01, 0x11, 0x00,
        0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x01, 0x00, 0x00,
    };
    std::vector<uint8_t> first = header;
    first.insert(first.end(), {0xFF, 0xD9});
    first.insert(first.end(), 512, 0x00);
    first.insert(first.end(), {0xFF, 0xD9});
    std::vector<uint8_t> second = header;
    second.insert(second.end(), {0xFF, 0xD9, 0xC0});
    second.insert(second.end(), 511, 0x00);
    second.insert(second.end(), {0xFF, 0xD9});
    ASSERT_EQ(first.size(), second.size());
    expectIdentityFollowsPicture(first, second);

    // Bytes after the scan the decoder never uses: the same picture, so the same identity (r5 A1; this assertion was
    // "differ" under the r8 byte walk, the loud direction A1 shows fails a healthy static clip as FROZEN_DISPLAY).
    const std::vector<uint8_t> plain = encodeLj92(2048, 12288);
    std::vector<uint8_t> padded = plain;
    padded.push_back(0x00);
    padded.push_back(0x7F);
    std::vector<uint16_t> plainPicture;
    std::vector<uint16_t> paddedPicture;
    ASSERT_TRUE(decodeLj92(plain, &plainPicture));
    ASSERT_TRUE(decodeLj92(padded, &paddedPicture));
    ASSERT_TRUE(plainPicture == paddedPicture);
    int status = -1;
    const uint64_t plainHash = lj92Hash(plain, &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(lj92Hash(padded, &status) == plainHash);
    ASSERT_EQ(0, status);
}

TEST(VisualCaptureSourceHash, Lj92APictureTheDecoderReadsDifferentlyHashesDifferently)
{
    int status = -1;
    const uint64_t first = lj92Hash(encodeLj92(2048, 12288), &status);
    ASSERT_EQ(0, status);
    const uint64_t second = lj92Hash(encodeLj92(2048, 12289), &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(first != second);
}

namespace
{
// r9 (adversary r3 on 9487c66f): hand-built 64x64 14-bit LJ92 headers. Z(n) = n zero bytes.
std::vector<uint8_t> bytes(std::initializer_list<std::vector<uint8_t>> parts)
{
    std::vector<uint8_t> out;
    for (const std::vector<uint8_t> & part : parts) out.insert(out.end(), part.begin(), part.end());
    return out;
}
std::vector<uint8_t> zeros(size_t n) { return std::vector<uint8_t>(n, 0); }
const std::vector<uint8_t> kSof3 = { 0xFF, 0xC3, 0x00, 0x0B, 0x0E, 0x00, 0x40, 0x00, 0x40, 0x01, 0x01, 0x11, 0x00 };
const std::vector<uint8_t> kSos = { 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x01, 0x00, 0x00 };
} // namespace

// r9 A1: the default liblj92 parseHuff does not advance past a DHT, so the next marker search walks the DHT's own bytes.
// An SOS spelled INSIDE a DHT's values is the scan the decoder uses; the bytes after it are the entropy data, and a
// difference there is a different picture (the old walk skipped the whole DHT and hashed only the later, equal data).
TEST(VisualCaptureSourceHash, Lj92AScanSpelledInsideADhtIsTheScanTheDecoderUses)
{
    const std::vector<uint8_t> n = bytes({ { 0xFF, 0xC4, 0x00, 0x15, 0x00, 0x02 }, zeros(15), { 0x00, 0x01 } });
    auto build = [&](uint8_t v)
    {
        const std::vector<uint8_t> embeddedDht = bytes({ { 0xFF, 0xC4, 0x02, 0x21, 0x00, 0x02 }, zeros(15), { 0x00, 0x01 },
                                                         kSos, { v }, zeros(511), { 0xFF, 0xD9 } });
        return bytes({ { 0xFF, 0xD8 }, kSof3, embeddedDht, n, kSof3, kSos, zeros(512), { 0xFF, 0xD9 } });
    };
    // r5: the identity follows the picture the decoder reads from the scan it uses (asserted against liblj92 itself).
    expectIdentityFollowsPicture(build(0x00), build(0xC0));
}

// r9 A2: a DHT value past sum(counts) is never read, so two payloads that differ only there are the same picture.
TEST(VisualCaptureSourceHash, Lj92ADhtValueBeyondTheDeclaredSymbolsIsNotHashed)
{
    auto build = [&](uint8_t unused)
    {
        return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x16, 0x00, 0x02 }, zeros(15), { 0x00, 0x01, unused },
                       kSof3, kSos, zeros(512), { 0xFF, 0xD9 } });
    };
    expectIdentityFollowsPicture(build(0x41), build(0x42));
    // ...while a DECLARED symbol counts exactly when the bitstream uses it (r5 A1): the all-zero entropy selects only
    // symbol 0, so symbol 1's value cannot change the picture; a leading 1 bit selects it.
    auto declared = [&](uint8_t value, uint8_t firstEntropyByte)
    {
        return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x16, 0x00, 0x02 }, zeros(15), { 0x00, value, 0x41 },
                       kSof3, kSos, { firstEntropyByte }, zeros(511), { 0xFF, 0xD9 } });
    };
    expectIdentityFollowsPicture(declared(0x01, 0x00), declared(0x02, 0x00));
    expectIdentityFollowsPicture(declared(0x01, 0x80), declared(0x02, 0x80));
}

// r9 (pass 2 A1 on 375023ab): an over-subscribed table (two 1-bit codes and a 2-bit code) fills the four-entry LUT with
// symbols 0,0,1,1 before it reaches the 2-bit level, so the third symbol (U) and the level-2 count are never read.
TEST(VisualCaptureSourceHash, Lj92AnOverSubscribedTablesUnreadSymbolIsNotHashed)
{
    auto build = [&](uint8_t unread)
    {
        return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x16, 0x00, 0x02, 0x01 }, zeros(14), { 0x00, 0x0D, unread },
                       kSof3, kSos, zeros(64), { 0xFF, 0xD9 } });
    };
    expectIdentityFollowsPicture(build(0x41), build(0x42));
    // The second symbol of the first level IS in the LUT (entries 2 and 3); it changes the picture, and so the identity,
    // only where the bitstream selects it (r5 A1).
    auto second_symbol = [&](uint8_t value, uint8_t firstEntropyByte)
    {
        return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x16, 0x00, 0x02, 0x01 }, zeros(14), { 0x00, value, 0x41 },
                       kSof3, kSos, { firstEntropyByte }, zeros(63), { 0xFF, 0xD9 } });
    };
    expectIdentityFollowsPicture(second_symbol(0x0D, 0x00), second_symbol(0x0E, 0x00));
    expectIdentityFollowsPicture(second_symbol(0x0D, 0x80), second_symbol(0x0E, 0x80));
}

// r5 (adversary r5 A1 on e57fd03e, verbatim input): a table symbol (U) the bitstream never selects. P(1) and P(2) decode
// to the same 64x64 two-level picture, so they are one source frame; the r9 byte walk hashed U and called them different,
// which fails a healthy static clip as FROZEN_DISPLAY (counted=false).
TEST(VisualCaptureSourceHash, Lj92ATableSymbolTheBitstreamNeverSelectsIsNotAnIdentity)
{
    // Each row: 32 zero bits (code 0 = diff 0), 10 (code 10 = 13 extra bits) 1000000000000 (+4096), 31 zero bits.
    std::vector<uint8_t> entropy;
    uint32_t acc = 0;
    int nbits = 0;
    auto put = [&](uint32_t value, int count)
    {
        for (int i = count - 1; i >= 0; --i)
        {
            acc = (acc << 1) | ((value >> i) & 1u);
            if (++nbits == 8)
            {
                entropy.push_back(static_cast<uint8_t>(acc));
                acc = 0;
                nbits = 0;
            }
        }
    };
    for (int row = 0; row < 64; ++row)
    {
        put(0, 32);
        put(0x2, 2);
        put(0x1000, 13);
        put(0, 31);
    }
    ASSERT_EQ(0, nbits);
    auto build = [&](uint8_t u)
    {
        return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x16, 0x00, 0x01, 0x02 }, zeros(14), { 0x00, 0x0D, u },
                       kSof3, kSos, entropy, { 0xFF, 0xD9 } });
    };
    ASSERT_EQ(static_cast<size_t>(675), build(0x01).size());
    std::vector<uint16_t> first;
    std::vector<uint16_t> second;
    ASSERT_TRUE(decodeLj92(build(0x01), &first));
    ASSERT_TRUE(decodeLj92(build(0x02), &second));
    ASSERT_TRUE(first == second);
    ASSERT_EQ(2, expectIdentityFollowsPicture(build(0x01), build(0x02)));
}

namespace
{
// r5 (opus r3 key B1 on 27da457e): SOI, SOF3, then a DHT marker whose length field (offset q = 17) starts with
// `atDhtLength`, zero-padded to 66000 bytes so the decoder's hufflen check (q + 0xFFxx < length) passes.
// liblj92's parseHuff zeroes the class/id byte (q + 2) in the buffer and does not advance, so the next marker
// search starts at q: an FF there makes the DHT length field a marker, and the byte after it is read as 0.
std::vector<uint8_t> classIdPayload(const std::vector<uint8_t> & atDhtLength)
{
    std::vector<uint8_t> out = bytes({ { 0xFF, 0xD8 }, kSof3, { 0xFF, 0xC4 }, atDhtLength });
    out.resize(66000, 0);
    return out;
}

// The key's repro: FF DA on the DHT length field. Entropy at q + 34 (scanstart q + 2 plus BEH(0, 0x20)).
std::vector<uint8_t> sosOnDhtLength(uint8_t classId, bool flip)
{
    std::vector<uint8_t> v = bytes({ { 0xFF, 0xDA, classId, 0x20, 0x01, 0x00, 0x00, 0x01 }, zeros(11), { 0x00, 0x01 },
                                     zeros(13), zeros(512) });
    if (flip) v[100] = 0x80;
    return classIdPayload(v);
}

// FF C3 on the DHT length field: an SOF3 of length BEH(0, 0x0B) at q + 2; SOS at q + 21, entropy at q + 31.
std::vector<uint8_t> sofOnDhtLength(uint8_t classId, bool flip)
{
    std::vector<uint8_t> v = bytes({ { 0xFF, 0xC3, classId, 0x0B, 0x0E, 0x00, 0x40, 0x00, 0x40, 0x01 }, zeros(9),
                                     { 0x00, 0x01 }, kSos, zeros(512) });
    if (flip) v[100] = 0x80;
    return classIdPayload(v);
}

// FF FE on the DHT length field: a COM of length BEH(0, 0x11) at q + 2; SOS at q + 21, entropy at q + 31.
std::vector<uint8_t> comOnDhtLength(uint8_t classId, bool flip)
{
    std::vector<uint8_t> v = bytes({ { 0xFF, 0xFE, classId, 0x11 }, zeros(15), { 0x00, 0x01 }, kSos, zeros(512) });
    if (flip) v[100] = 0x80;
    return classIdPayload(v);
}

// FF C4 on the DHT length field: a second DHT at q + 2 (length BEH(0, 0xDA), its own class/id at q + 4 zeroed too);
// its values at q + 21; SOS at q + 23, entropy at q + 33. A class/id of FF is NOT a marker to the decoder (it reads 0).
std::vector<uint8_t> dhtOnDhtLength(uint8_t classId, bool flip)
{
    std::vector<uint8_t> v = bytes({ { 0xFF, 0xC4, classId, 0xDA, 0x00, 0x02 }, zeros(15), { 0x00, 0x01 }, kSos,
                                     zeros(512) });
    if (flip) v[100] = 0x80;
    return classIdPayload(v);
}
} // namespace

// r5 B1, the key's repro verbatim: the decoder's entropy starts at q + 34; the old walk read the ORIGINAL class/id (01)
// as the SOS length's high byte and started 256 bytes later, so A and B -- different pictures -- hashed equal.
TEST(VisualCaptureSourceHash, Lj92AScanWhoseLengthIsTheZeroedClassIdStartsWhereTheDecoderStarts)
{
    const std::vector<uint8_t> a = sosOnDhtLength(0x01, false);
    const std::vector<uint8_t> b = sosOnDhtLength(0x01, true);
    std::vector<uint16_t> aPicture;
    std::vector<uint16_t> bPicture;
    ASSERT_TRUE(decodeLj92(a, &aPicture));
    ASSERT_TRUE(decodeLj92(b, &bPicture));
    ASSERT_TRUE(aPicture != bPicture); // the decoder reads byte q + 100...
    int status = -1;
    const uint64_t aHash = lj92Hash(a, &status);
    ASSERT_EQ(0, status);
    const uint64_t bHash = lj92Hash(b, &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(aHash != bHash); // ...so "identical" would waive FROZEN_DISPLAY over two different pictures
    // The const entry point walks a copy: the caller's payload is not written.
    ASSERT_TRUE(a == sosOnDhtLength(0x01, false));
}

// r5 B1 class sweep: FF DA / FF C3 / FF FE / FF C4 on the DHT length field. The decoder reads the class/id byte as 0
// whatever it was, so every class/id gives the class/id-0 picture and identity; a byte the decoder reads still counts.
TEST(VisualCaptureSourceHash, Lj92TheDhtClassIdIsReadAsZeroWhereverTheWalkLandsOnIt)
{
    using Build = std::vector<uint8_t> (*)(uint8_t, bool);
    const Build builds[] = { sosOnDhtLength, sofOnDhtLength, comOnDhtLength, dhtOnDhtLength };
    for (const Build build : builds)
    {
        std::vector<uint16_t> zeroPicture;
        ASSERT_TRUE(decodeLj92(build(0x00, false), &zeroPicture));
        int status = -1;
        const uint64_t zeroHash = lj92Hash(build(0x00, false), &status);
        ASSERT_EQ(0, status);
        for (const uint8_t classId : std::initializer_list<uint8_t>{ 0x01, 0x02, 0x7F, 0xFF })
        {
            std::vector<uint16_t> picture;
            ASSERT_TRUE(decodeLj92(build(classId, false), &picture));
            ASSERT_TRUE(picture == zeroPicture);
            ASSERT_TRUE(lj92Hash(build(classId, false), &status) == zeroHash);
            ASSERT_EQ(0, status);

            std::vector<uint16_t> flipped;
            ASSERT_TRUE(decodeLj92(build(classId, true), &flipped));
            ASSERT_TRUE(flipped != zeroPicture);
            ASSERT_TRUE(lj92Hash(build(classId, true), &status) != zeroHash);
            ASSERT_EQ(0, status);
        }
    }
}

// r5 (adversary r5 pass 2 A1 on 787b70cc, verbatim input): a 64x64 and a 128x32 SOF3 over one scan decode the same
// sample sequence, which the display reads in RAWI's geometry either way, so the identity hashes the samples, not the
// LJ92 header's shape.
TEST(VisualCaptureSourceHash, Lj92TheHeadersShapeIsNotPartOfTheIdentity)
{
    auto build = [&](const std::vector<uint8_t> & sof)
    {
        return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x15, 0x00, 0x01, 0x01 }, zeros(14), { 0x00, 0x0D }, sof, kSos,
                       zeros(256), { 0xA0 }, zeros(256), { 0x03 }, { 0xFF, 0xD9 } });
    };
    const std::vector<uint8_t> square = build(kSof3);
    const std::vector<uint8_t> wide = build({ 0xFF, 0xC3, 0x00, 0x0B, 0x0E, 0x00, 0x20, 0x00, 0x80, 0x01, 0x01, 0x11, 0x00 });
    ASSERT_EQ(static_cast<size_t>(564), square.size());
    ASSERT_EQ(2, expectIdentityFollowsPicture(square, wide));
}

// r5 (adversary r5 pass 2 A2 on 787b70cc, verbatim input): a 114-byte payload whose scan runs out of entropy bytes long
// before its 4096 samples. The decode copy is sized like the display decode's buffer (max(length, packed) + 4, zeroed),
// so the bit reader's run past the payload stays inside it; the identity still follows the decoded picture.
TEST(VisualCaptureSourceHash, Lj92AShortScanDecodesInsideTheDisplaySizedCopy)
{
    const std::vector<uint8_t> shortScan = bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x15, 0x00, 0x02 }, zeros(15), { 0x00, 0x00 },
                                                   kSof3, kSos, zeros(64), { 0xFF, 0xD9 } });
    ASSERT_EQ(static_cast<size_t>(114), shortScan.size());
    std::vector<uint8_t> padded = shortScan;
    padded.push_back(0x00); // one more zero byte the decoder may read as entropy: the same picture or not, per liblj92
    expectIdentityFollowsPicture(shortScan, padded);
}

namespace
{
// r6 (adversary r5 pass 3 A1/A2 on e747c5a4, verbatim): Z(n) = n zero bytes. A 64x64 14-bit LJ92 frame, one 1-bit code per
// category (0: diff 0, 1: diff +-1), predictor 1, whose 64 entropy bytes hold 512 of its 4096 samples: the scan runs out.
std::vector<uint8_t> shortScanPayload()
{
    return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x15, 0x00, 0x02 }, zeros(15), { 0x00, 0x01 }, kSof3, kSos, zeros(64) });
}

// lj92_open's verdict on a COPY of `payload` (datalen = its size) followed by `slack` zero bytes; the handle is closed.
int lj92OpenStatus(std::vector<uint8_t> payload, size_t slack)
{
    const int length = static_cast<int>(payload.size());
    payload.resize(payload.size() + slack, 0);
    lj92 decoder = nullptr;
    int width = 0, height = 0, bitdepth = 0, components = 0;
    const int ret = lj92_open(&decoder, payload.data(), length, &width, &height, &bitdepth, &components);
    if (ret == LJ92_ERROR_NONE) lj92_close(decoder);
    return ret;
}

// The display decode's call (getMlvRawFrameUint16Direct: lj92_open with datalen = frame_size, then lj92_decode of every
// sample) on a buffer holding `payload` and then `tail`, the bytes the display's raw_frame happens to hold after the read.
int decodeWithTail(std::vector<uint8_t> payload, const std::vector<uint8_t> & tail, std::vector<uint16_t> * picture)
{
    const int length = static_cast<int>(payload.size());
    payload.insert(payload.end(), tail.begin(), tail.end());
    lj92 decoder = nullptr;
    int width = 0, height = 0, bitdepth = 0, components = 0;
    int ret = lj92_open(&decoder, payload.data(), length, &width, &height, &bitdepth, &components);
    if (ret != LJ92_ERROR_NONE) return ret;
    picture->assign(static_cast<size_t>(width) * height * components, 0);
    ret = lj92_decode(decoder, picture->data(), width * height * components, 0, nullptr, 0);
    lj92_close(decoder);
    return ret;
}

// FNV-1a 64 over the samples, low byte first: mlvLj92FrameSourceHash64's identity of a decoded picture.
uint64_t pictureIdentity(const std::vector<uint16_t> & picture)
{
    uint64_t hash = 0xcbf29ce484222325ull;
    for (const uint16_t sample : picture)
        for (const uint8_t byte : { static_cast<uint8_t>(sample & 0xFFu), static_cast<uint8_t>(sample >> 8) })
        {
            hash ^= byte;
            hash *= 0x100000001b3ull;
        }
    return hash;
}
} // namespace

// r6 (adversary r5 pass 3 A2 on e747c5a4, verbatim input): A1's 112-byte payload with the SOS length (bytes 40, 41) set to
// FF FF. lj92_open accepted it and every scan variant started its bit reader at scanstart + 65535, past the allocation.
// liblj92 now refuses it, so the display decode, the source identity and the fuzz harness all refuse it.
TEST(VisualCaptureSourceHash, Lj92AScanHeaderLongerThanThePayloadIsRefused)
{
    std::vector<uint8_t> payload = shortScanPayload();
    ASSERT_EQ(static_cast<size_t>(112), payload.size());
    ASSERT_EQ(0xDA, payload[39]);
    payload[40] = 0xFF;
    payload[41] = 0xFF;
    ASSERT_NE(LJ92_ERROR_NONE, lj92OpenStatus(payload, 7172 - 112 + 65536)); // refused before any scan is read
    int status = 0;
    lj92Hash(payload, &status);
    ASSERT_NE(0, status);

    // The class: every marker-segment length field, and every segment end, lies inside the payload.
    const std::vector<uint8_t> sof3Head = { 0xFF, 0xC3, 0x00, 0x0B, 0x0E, 0x00, 0x40, 0x00, 0x40 };
    const std::vector<std::vector<uint8_t>> refused = {
        // the SOS ends exactly at the payload end: no entropy byte inside it
        bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x15, 0x00, 0x02 }, zeros(15), { 0x00, 0x01 }, kSof3, kSos }),
        // the SOS length field is cut after its first byte
        bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x15, 0x00, 0x02 }, zeros(15), { 0x00, 0x01 }, kSof3, { 0xFF, 0xDA, 0x00 } }),
        // a COM, an APP1 and an SOF3 whose lengths (FF FF) run past the payload
        withSegmentAfterSoi(shortScanPayload(), { 0xFF, 0xFE, 0xFF, 0xFF }),
        withSegmentAfterSoi(shortScanPayload(), { 0xFF, 0xE1, 0xFF, 0xFF }),
        bytes({ { 0xFF, 0xD8, 0xFF, 0xC3, 0xFF, 0xFF, 0x0E, 0x00, 0x40, 0x00, 0x40, 0x01, 0x01, 0x11, 0x00 }, zeros(8) }),
        // an SOF3 cut before its component count (byte ix + 7)
        bytes({ { 0xFF, 0xD8 }, sof3Head }),
        // a DHT whose 16 counts are cut, and one whose declared symbols run past the payload
        bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x15, 0x00, 0x02 }, zeros(5) }),
        bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x03, 0x00, 0x02 }, zeros(14), { 0xFF } }),
        // an SOS whose predictor byte (scanstart + 3 + 2 * component count) is past the payload
        bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x15, 0x00, 0x02 }, zeros(15), { 0x00, 0x01 }, kSof3,
                { 0xFF, 0xDA, 0x00, 0x02, 0xFF }, zeros(8) }),
    };
    for (const std::vector<uint8_t> & bad : refused)
    {
        ASSERT_NE(LJ92_ERROR_NONE, lj92OpenStatus(bad, 65536));
        if (bad.size() >= 4)
        {
            lj92Hash(bad, &status);
            ASSERT_NE(0, status);
        }
    }
}

// r6 (adversary r5 pass 3 A1 on e747c5a4, verbatim input): one payload, two display decodes. The scan runs out of its 112
// bytes after 512 samples; the display's raw_frame is max(frame_size, packed) + 4 = 7172 bytes, read up to frame_size and
// (at e747c5a4) left uninitialised after it. Frame 1's tail is zero, frame 2's is zero except byte 112 = 0x80: the bit
// reader read that byte and the two calls showed different pictures while the identity (decoded over a zeroed tail) was
// one. liblj92 now reads zero bits past datalen, so the tail cannot change the picture (and the display zeroes it anyway).
TEST(VisualCaptureSourceHash, Lj92AScanThatRunsPastThePayloadIsOnePictureWhateverFollowsIt)
{
    const std::vector<uint8_t> payload = shortScanPayload();
    ASSERT_EQ(static_cast<size_t>(112), payload.size());
    std::vector<uint8_t> zeroTail(7172 - 112, 0x00);
    std::vector<uint8_t> markedTail = zeroTail;
    markedTail[0] = 0x80;
    std::vector<uint8_t> noisyTail(7172 - 112, 0x00);
    for (size_t i = 0; i < noisyTail.size(); ++i) noisyTail[i] = static_cast<uint8_t>(0x5Bu * (i + 1u) + (i >> 3));

    std::vector<uint16_t> first;
    ASSERT_EQ(LJ92_ERROR_NONE, decodeWithTail(payload, zeroTail, &first));
    ASSERT_EQ(static_cast<size_t>(4096), first.size());
    std::vector<uint16_t> second;
    ASSERT_EQ(LJ92_ERROR_NONE, decodeWithTail(payload, markedTail, &second));
    ASSERT_TRUE(first == second); // same payload, same picture, two calls
    std::vector<uint16_t> third;
    ASSERT_EQ(LJ92_ERROR_NONE, decodeWithTail(payload, noisyTail, &third));
    ASSERT_TRUE(first == third);
    std::vector<uint16_t> bare;
    ASSERT_EQ(LJ92_ERROR_NONE, decodeWithTail(payload, {}, &bare)); // no slack at all: nothing past the payload is read
    ASSERT_TRUE(first == bare);

    // The identity is the picture the display shows.
    int status = -1;
    const uint64_t identity = lj92Hash(payload, &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(identity == pictureIdentity(second));

    // The class: every scan variant (predictor 1 fast path, the generic scan via predictor 2, predictor 6) gives one
    // verdict and, when it decodes, one picture, whatever follows the payload.
    for (const uint8_t predictor : std::initializer_list<uint8_t>{ 1, 2, 6 })
    {
        std::vector<uint8_t> variant = payload;
        ASSERT_EQ(0x01, variant[45]); // kSos's Ss (predictor) byte
        variant[45] = predictor;
        std::vector<uint16_t> a;
        std::vector<uint16_t> b;
        const int aStatus = decodeWithTail(variant, zeroTail, &a);
        const int bStatus = decodeWithTail(variant, markedTail, &b);
        ASSERT_EQ(aStatus, bStatus);
        if (aStatus == LJ92_ERROR_NONE) ASSERT_TRUE(a == b);
        const int cStatus = decodeWithTail(variant, noisyTail, &b);
        ASSERT_EQ(aStatus, cStatus);
        if (aStatus == LJ92_ERROR_NONE) ASSERT_TRUE(a == b);
    }
}

// r6 (A1 class, found by the r6 sweep): lookup-table entries liblj92 never wrote. A table with no codes at all wrote none
// (every sample read uninitialised memory) and is refused; a code the table leaves undefined (here '1', with only '0'
// declared) decodes the same way on every call.
TEST(VisualCaptureSourceHash, Lj92LookupEntriesTheTableNeverDefinesAreNeverReadUninitialised)
{
    const std::vector<uint8_t> noCodes = bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x13, 0x00 }, zeros(16), kSof3, kSos,
                                                 zeros(64), { 0xFF, 0xD9 } });
    ASSERT_NE(LJ92_ERROR_NONE, lj92OpenStatus(noCodes, 65536));
    int status = 0;
    lj92Hash(noCodes, &status);
    ASSERT_NE(0, status);

    const std::vector<uint8_t> undefinedCode = bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x14, 0x00, 0x01 }, zeros(15),
                                                       { 0x00 }, kSof3, kSos, { 0x80 }, zeros(511), { 0xFF, 0xD9 } });
    std::vector<uint16_t> first;
    std::vector<uint16_t> second;
    ASSERT_EQ(LJ92_ERROR_NONE, decodeWithTail(undefinedCode, {}, &first));
    ASSERT_EQ(LJ92_ERROR_NONE, decodeWithTail(undefinedCode, {}, &second));
    ASSERT_TRUE(first == second);
    ASSERT_TRUE(lj92Hash(undefinedCode, &status) == pictureIdentity(first));
    ASSERT_EQ(0, status);
}

namespace
{
// The display's call into a target that already holds `fill` in every sample (the display's unpackedFrame keeps the
// previous frame): a decode that reports success must have written every sample it reported.
int decodeOverFill(const std::vector<uint8_t> & payload, uint16_t fill, std::vector<uint16_t> * picture)
{
    std::vector<uint8_t> copy = payload;
    lj92 decoder = nullptr;
    int width = 0, height = 0, bitdepth = 0, components = 0;
    int ret = lj92_open(&decoder, copy.data(), static_cast<int>(copy.size()), &width, &height, &bitdepth, &components);
    if (ret != LJ92_ERROR_NONE) return ret;
    picture->assign(static_cast<size_t>(width) * height * components, fill);
    ret = lj92_decode(decoder, picture->data(), width * height * components, 0, nullptr, 0);
    lj92_close(decoder);
    return ret;
}

// r6 pass 1 A1 (adversary on be8e2c33, verbatim): a 32x64 two-component SOF3 (4096 samples, RAWI 64x64) whose SOS asks
// for predictor 6, with one 1-bit code (diff 0) and 512 zero entropy bytes.
std::vector<uint8_t> twoComponentPayload(uint8_t predictor)
{
    return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x14, 0x00, 0x01 }, zeros(15), { 0x00 },
                   { 0xFF, 0xC3, 0x00, 0x0E, 0x0E, 0x00, 0x40, 0x00, 0x20, 0x02, 0x01, 0x11, 0x00, 0x02, 0x11, 0x00 },
                   { 0xFF, 0xDA, 0x00, 0x0A, 0x02, 0x01, 0x00, 0x02, 0x00, predictor, 0x00, 0x00 }, zeros(512),
                   { 0xFF, 0xD9 } });
}
} // namespace

// r6 pass 1 A1 (adversary on be8e2c33, verbatim input): liblj92's predictor-6 fast path decoded x * y samples whatever the
// component count and returned success, so a two-component frame left half the display's samples as the previous frame
// held them (1000 in one call, 2000 in the next) while the identity hashed zeros there. Every scan variant now writes
// every sample it reports success for, so the picture, and the identity, do not depend on what the target held.
TEST(VisualCaptureSourceHash, Lj92ADecodeThatSucceedsWritesEverySample)
{
    const std::vector<uint8_t> payload = twoComponentPayload(6);
    ASSERT_EQ(static_cast<size_t>(566), payload.size());
    std::vector<uint16_t> first;
    std::vector<uint16_t> second;
    ASSERT_EQ(LJ92_ERROR_NONE, decodeOverFill(payload, 1000, &first));
    ASSERT_EQ(LJ92_ERROR_NONE, decodeOverFill(payload, 2000, &second));
    ASSERT_EQ(static_cast<size_t>(4096), first.size());
    ASSERT_TRUE(first == second);
    int status = -1;
    ASSERT_TRUE(lj92Hash(payload, &status) == pictureIdentity(first));
    ASSERT_EQ(0, status);

    // The class: every predictor, one and two components.
    for (int predictor = 1; predictor <= 7; ++predictor)
    {
        std::vector<uint8_t> oneComponent = bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x14, 0x00, 0x01 }, zeros(15), { 0x00 },
                                                    kSof3, kSos, zeros(512), { 0xFF, 0xD9 } });
        oneComponent[oneComponent.size() - 512 - 2 - 3] = static_cast<uint8_t>(predictor); // kSos's Ss byte
        for (const std::vector<uint8_t> & variant : { twoComponentPayload(static_cast<uint8_t>(predictor)), oneComponent })
        {
            std::vector<uint16_t> a;
            std::vector<uint16_t> b;
            const int aStatus = decodeOverFill(variant, 1000, &a);
            const int bStatus = decodeOverFill(variant, 2000, &b);
            ASSERT_EQ(aStatus, bStatus);
            if (aStatus != LJ92_ERROR_NONE) continue;
            ASSERT_TRUE(a == b);
            ASSERT_TRUE(lj92Hash(variant, &status) == pictureIdentity(a));
            ASSERT_EQ(0, status);
        }
    }
}

namespace
{
// r6 pass 2: a small 14-bit LJ92 frame of `components` interleaved components, `width` columns and `height` rows, with
// codes '0' (diff 0) and '10' + 2 bits (diff +-2/+-3), predictor `predictor`, entropy = `diffs` encoded in sample order.
std::vector<uint8_t> smallFrame(int width, int height, int components, uint8_t predictor, const std::vector<int> & diffs)
{
    std::vector<uint8_t> entropy;
    uint32_t acc = 0;
    int nbits = 0;
    auto put = [&](uint32_t value, int count)
    {
        for (int i = count - 1; i >= 0; --i)
        {
            acc = (acc << 1) | ((value >> i) & 1u);
            if (++nbits == 8)
            {
                entropy.push_back(static_cast<uint8_t>(acc)); // never FF: no code has 8 ones in a row
                acc = 0;
                nbits = 0;
            }
        }
    };
    for (const int diff : diffs)
    {
        if (diff == 0) put(0, 1);
        else
        {
            put(0x2, 2);
            put(static_cast<uint32_t>(diff > 0 ? diff : diff + 3), 2);
        }
    }
    if (nbits) put(0, 8 - nbits);
    entropy.insert(entropy.end(), 4, 0x00);
    std::vector<uint8_t> sof = { 0xFF, 0xC3, 0x00, static_cast<uint8_t>(8 + 3 * components), 0x0E,
                                 static_cast<uint8_t>(height >> 8), static_cast<uint8_t>(height),
                                 static_cast<uint8_t>(width >> 8), static_cast<uint8_t>(width),
                                 static_cast<uint8_t>(components) };
    std::vector<uint8_t> sos = { 0xFF, 0xDA, 0x00, static_cast<uint8_t>(6 + 2 * components), static_cast<uint8_t>(components) };
    for (int c = 0; c < components; ++c)
    {
        sof.insert(sof.end(), { static_cast<uint8_t>(c + 1), 0x11, 0x00 });
        sos.insert(sos.end(), { static_cast<uint8_t>(c + 1), 0x00 });
    }
    sos.insert(sos.end(), { predictor, 0x00, 0x00 });
    return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x15, 0x00, 0x01, 0x01 }, zeros(14), { 0x00, 0x02 }, sof, sos, entropy,
                   { 0xFF, 0xD9 } });
}

// Lossless JPEG (ITU T.81 H.1.2.1) with liblj92's first-row / first-column rules, each component predicted from its own
// neighbours: Ra left, Rb above, Rc above-left.
std::vector<uint16_t> referencePicture(int width, int height, int components, int predictor, const std::vector<int> & diffs)
{
    std::vector<uint16_t> out(static_cast<size_t>(width) * height * components, 0);
    size_t k = 0;
    auto at = [&](int row, int col, int c) -> int { return out[(static_cast<size_t>(row) * width + col) * components + c]; };
    for (int row = 0; row < height; ++row)
        for (int col = 0; col < width; ++col)
            for (int c = 0; c < components; ++c)
            {
                int px;
                if (row == 0 && col == 0) px = 1 << 13;
                else if (row == 0) px = at(row, col - 1, c);
                else if (col == 0) px = at(row - 1, col, c);
                else
                {
                    const int ra = at(row, col - 1, c), rb = at(row - 1, col, c), rc = at(row - 1, col - 1, c);
                    switch (predictor)
                    {
                    case 1: px = ra; break;
                    case 2: px = rb; break;
                    case 3: px = rc; break;
                    case 4: px = ra + rb - rc; break;
                    case 5: px = ra + ((rb - rc) >> 1); break;
                    case 6: px = rb + ((ra - rc) >> 1); break;
                    default: px = (ra + rb) >> 1; break;
                    }
                }
                out[(static_cast<size_t>(row) * width + col) * components + c] = static_cast<uint16_t>(px + diffs[k++]);
            }
    return out;
}
} // namespace

// r6 pass 2 A1 (adversary on f1659d6a, verbatim input): the generic scan predicted with the previous sample of ANY
// component (scalar `left`) where predictors 4-7 need the same component's left neighbour; a 2x4 two-component
// predictor-6 frame decoded [8193,8193] where the picture is [8192,8194]. r6 pass 2 A2 (advisory, pre-existing,
// verbatim input): the second row buffer started x samples in, not x * components, so a two-component predictor-2 frame
// overwrote the row above before reading it. Both now decode the reference picture, for every predictor and both
// component counts (the predictor-1 and predictor-6 fast paths included), and the identity is that picture's.
TEST(VisualCaptureSourceHash, Lj92EveryComponentIsPredictedFromItsOwnNeighbours)
{
    const std::vector<uint8_t> a1 = bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x15, 0x00, 0x01, 0x01 }, zeros(14), { 0x00, 0x02 },
                                            { 0xFF, 0xC3, 0x00, 0x0E, 0x0E, 0x00, 0x04, 0x00, 0x02, 0x02, 0x01, 0x11, 0x00, 0x02, 0x11, 0x00 },
                                            { 0xFF, 0xDA, 0x00, 0x0A, 0x02, 0x01, 0x00, 0x02, 0x00, 0x06, 0x00, 0x00 },
                                            { 0x50, 0x00, 0x00, 0xFF, 0xD9 } });
    ASSERT_EQ(static_cast<size_t>(58), a1.size());
    std::vector<uint16_t> picture;
    ASSERT_EQ(LJ92_ERROR_NONE, decodeOverFill(a1, 0, &picture));
    const std::vector<uint16_t> a1Expected = { 8192, 8194, 8192, 8194, 8192, 8194, 8192, 8194,
                                               8192, 8194, 8192, 8194, 8192, 8194, 8192, 8194 };
    ASSERT_TRUE(picture == a1Expected);

    std::vector<uint8_t> a2 = a1;
    a2[a2.size() - 5 - 3] = 0x02; // predictor 2
    a2[a2.size() - 5] = 0x2A;
    a2[a2.size() - 4] = 0x80;
    ASSERT_EQ(LJ92_ERROR_NONE, decodeOverFill(a2, 0, &picture));
    const std::vector<uint16_t> a2Expected = { 8192, 8192, 8194, 8194, 8192, 8192, 8194, 8194,
                                               8192, 8192, 8194, 8194, 8192, 8192, 8194, 8194 };
    ASSERT_TRUE(picture == a2Expected);

    // The class, against the reference decoder.
    const int pattern[] = { 0, 2, -3, 3, 0, -2, 2, 0, 3, -3, 0, 0, -2, 2, 3 };
    const int shapes[][3] = { { 4, 4, 1 }, { 4, 4, 2 }, { 1, 4, 2 }, { 3, 2, 2 } };
    for (const auto & shape : shapes)
    {
        const int width = shape[0], height = shape[1], components = shape[2];
        std::vector<int> diffs(static_cast<size_t>(width) * height * components);
        for (size_t i = 0; i < diffs.size(); ++i) diffs[i] = pattern[(i * 7u + 3u) % 15u];
        for (int predictor = 1; predictor <= 7; ++predictor)
        {
            const std::vector<uint8_t> frame = smallFrame(width, height, components, static_cast<uint8_t>(predictor), diffs);
            std::vector<uint16_t> decoded;
            ASSERT_EQ(LJ92_ERROR_NONE, decodeOverFill(frame, 0x5A5A, &decoded));
            ASSERT_TRUE(decoded == referencePicture(width, height, components, predictor, diffs));
            int status = -1;
            ASSERT_TRUE(lj92Hash(frame, &status) == pictureIdentity(decoded));
            ASSERT_EQ(0, status);
        }
    }
}

// r6 pass 1 A2 (advisory, pre-existing, verbatim input): an LJ92 payload with no DHT opened, and the first sample read the
// lookup table liblj92 never allocated (a NULL dereference). It is refused, so the display and the identity refuse it.
TEST(VisualCaptureSourceHash, Lj92APayloadWithoutAHuffmanTableIsRefused)
{
    const std::vector<uint8_t> payload = bytes({ { 0xFF, 0xD8 }, kSof3, kSos, zeros(512), { 0xFF, 0xD9 } });
    ASSERT_EQ(static_cast<size_t>(539), payload.size());
    ASSERT_NE(LJ92_ERROR_NONE, lj92OpenStatus(payload, 0));
    int status = 0;
    lj92Hash(payload, &status);
    ASSERT_NE(0, status);
}

TEST(VisualCaptureSourceHash, Lj92APayloadTheDecoderRefusesHasNoIdentity)
{
    const std::vector<uint8_t> plain = encodeLj92(2048, 12288);
    ASSERT_TRUE(plain.size() > 16);
    int status = 0;
    std::vector<uint8_t> noSoi = plain;
    noSoi[1] = 0xD9;
    lj92Hash(noSoi, &status);
    ASSERT_NE(0, status);
    lj92Hash({0xFF, 0xD8, 0xFF, 0xD9, 0x00}, &status); // EOI before any scan
    ASSERT_NE(0, status);
    // r5: a COM of length 1 is NOT refused by liblj92 (parseBlock steps one byte and the marker search finds the DHT),
    // so it is no longer "no identity" (the r9 walk refused it): it decodes the plain picture and has its identity.
    ASSERT_EQ(2, expectIdentityFollowsPicture(withSegmentAfterSoi(plain, {0xFF, 0xFE, 0x00, 0x01}), plain));
    uint64_t hash = 0;
    ASSERT_NE(0, mlvLj92FrameSourceHash64(nullptr, 8, &hash));
    ASSERT_NE(0, mlvLj92FrameSourceHash64(plain.data(), plain.size(), nullptr));
}

TEST(VisualCaptureSourceHash, OneFrameHashesTheSameTwice)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    uint64_t first = 0;
    uint64_t second = 0;
    ASSERT_EQ(0, getMlvFrameSourceHash64(fixture.video(), 0, &first));
    ASSERT_EQ(0, getMlvFrameSourceHash64(fixture.video(), 0, &second));
    ASSERT_TRUE(first == second);
    // FNV-1a 64's offset basis is the hash of zero bytes: a real read moved it.
    ASSERT_TRUE(first != 0xcbf29ce484222325ull);
}

TEST(VisualCaptureSourceHash, TwoFramesOfTheClipHashDifferently)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    const uint64_t frames = getMlvFrames(fixture.video());
    ASSERT_TRUE(frames > 1);
    uint64_t firstFrame = 0;
    uint64_t lastFrame = 0;
    ASSERT_EQ(0, getMlvFrameSourceHash64(fixture.video(), 0, &firstFrame));
    ASSERT_EQ(0, getMlvFrameSourceHash64(fixture.video(), frames - 1, &lastFrame));
    ASSERT_TRUE(firstFrame != lastFrame);
}

TEST(VisualCaptureSourceHash, AnIndexPastTheLastFrameHasNoIdentity)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    uint64_t hash = 0x5a5a5a5aull;
    ASSERT_NE(0, getMlvFrameSourceHash64(fixture.video(), getMlvFrames(fixture.video()), &hash));
}

TEST(VisualCaptureSourceHash, NullInputsHaveNoIdentity)
{
    uint64_t hash = 0;
    ASSERT_NE(0, getMlvFrameSourceHash64(nullptr, 0, &hash));
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    ASSERT_NE(0, getMlvFrameSourceHash64(fixture.video(), 0, nullptr));
}

// r8 (fable r7 key A2 = Dot B2 on 09973e83): the JPEG2K branch hashed every frame_size byte, but mlvJpeg2kBayerLayoutIsValid only
// requires each channel span to lie inside the frame, and the display decode reads the 36-byte header and the four declared spans,
// nothing else. The key's input: LE32[1,36,n,36+n,n,36+2n,n,36+3n,n] + C x4 + one trailing byte 00 vs 01: one picture, two ids,
// "differ", FROZEN_DISPLAY counted=false on a static JPEG2K clip.
// r9 (r8 adversary pass 2 A1 = Dot B2 surviving r8, on e139944c): a span's own bytes are not the picture either. OpenJPH skips a
// main-header COM segment (ojph_codestream_local.cpp read_headers, marker 13 -> skip_marker "COM"), so two codestreams that differ
// only in an equal-length comment decode to one picture, yet r8 gave them two ids. The identity is now the DECODED picture (as
// LJ92's is). Every fixture below is a REAL codestream made by the vendored OpenJPH encoder and decoded by the real OpenJPH decoder
// (OjphRealCodecScope; tests/pipeline/ojph_real_codec.cpp). The r8 tests that still describe true behaviour (bytes outside the spans,
// where a span lies) are kept on real codestreams; the two that pinned span-bytes hashing (a changed span byte differs; the
// in-memory hash is the header plus the spans) are rewritten to the decoded picture.
namespace
{

// A 64x64 RAWI frame: four 32x32 quarter channels. 14-bit samples, like the adversary's input.
constexpr uint32_t kJ2kQuarter = 32;
constexpr int kJ2kFrameSide = 64;

// A two-level quarter picture (x < 16 ? left : right): not flat, so a decode that ignored the codestream could not pass.
std::vector<int32_t> j2kQuarterPicture(int32_t left, int32_t right)
{
    std::vector<int32_t> picture(kJ2kQuarter * kJ2kQuarter);
    for (uint32_t y = 0; y < kJ2kQuarter; ++y)
        for (uint32_t x = 0; x < kJ2kQuarter; ++x)
            picture[y * kJ2kQuarter + x] = x < 16 ? left : right;
    return picture;
}

// The vendored OpenJPH encoder (the product wrapper, ojph_real_*): one component, 14-bit, lossless, no decompositions.
std::vector<uint8_t> encodeJ2kQuarter(const std::vector<int32_t> & picture)
{
    ASSERT_EQ(static_cast<size_t>(kJ2kQuarter) * kJ2kQuarter, picture.size());
    void * encoder = ojph_real_encoder_new();
    ASSERT_TRUE(encoder != nullptr);
    ojph_real_encoder_set_image(encoder, kJ2kQuarter, kJ2kQuarter, 1, 14, 0);
    ojph_real_encoder_set_lossless(encoder, 1);
    ojph_real_encoder_set_decompositions(encoder, 0);
    std::vector<uint8_t> out(65536, 0);
    const size_t written = ojph_real_encoder_encode_into(encoder, picture.data(), out.data(), out.size());
    ojph_real_encoder_free(encoder);
    ASSERT_TRUE(written > 0 && written < out.size());
    out.resize(written);
    return out;
}

// The real OpenJPH decoder on one codestream (what the display decode runs per channel).
std::vector<int32_t> decodeJ2kQuarter(const std::vector<uint8_t> & codestream)
{
    void * decoder = ojph_real_decoder_new();
    ASSERT_TRUE(decoder != nullptr);
    std::vector<int32_t> picture(kJ2kQuarter * kJ2kQuarter, -1);
    uint32_t w = 0, h = 0, components = 0;
    const size_t decoded = ojph_real_decoder_decode_into(decoder, codestream.data(), codestream.size(), picture.data(),
                                                         picture.size(), &w, &h, &components);
    ojph_real_decoder_free(decoder);
    ASSERT_EQ(picture.size(), decoded);
    ASSERT_TRUE(w == kJ2kQuarter && h == kJ2kQuarter && components == 1u);
    return picture;
}

// The index of the last payload byte of the encoder's main-header COM segment (FF 64, Lcom, Rcom, "      OpenJPH Ver a.b.c."),
// which must lie before the first tile-part (SOT, FF 90).
size_t j2kMainHeaderComLastByte(const std::vector<uint8_t> & codestream)
{
    size_t com = 0;
    while (com + 1 < codestream.size() && !(codestream[com] == 0xFF && codestream[com + 1] == 0x64)) ++com;
    ASSERT_TRUE(com + 6 < codestream.size());
    size_t sot = 0;
    while (sot + 1 < codestream.size() && !(codestream[sot] == 0xFF && codestream[sot + 1] == 0x90)) ++sot;
    ASSERT_TRUE(com < sot && sot + 1 < codestream.size());
    const size_t length = (static_cast<size_t>(codestream[com + 2]) << 8) | codestream[com + 3];   // Lcom: from Lcom to the end
    ASSERT_TRUE(length > 4 && com + 2 + length <= sot);
    const std::string comment(codestream.begin() + static_cast<std::ptrdiff_t>(com + 6),
                              codestream.begin() + static_cast<std::ptrdiff_t>(com + 2 + length));
    ASSERT_TRUE(comment.find("OpenJPH Ver") != std::string::npos);
    return com + 2 + length - 1;
}

// The codestream's twin: the same length, only the COM comment's final byte changed (the adversary's 0x2e '.' -> 0x2f '/').
std::vector<uint8_t> j2kComTwin(const std::vector<uint8_t> & codestream)
{
    std::vector<uint8_t> twin = codestream;
    const size_t last = j2kMainHeaderComLastByte(codestream);
    ASSERT_EQ(static_cast<uint8_t>(0x2e), twin[last]);
    twin[last] = 0x2f;
    return twin;
}

void putLe32(std::vector<uint8_t> & out, size_t at, uint32_t value)
{
    for (int i = 0; i < 4; ++i) out[at + i] = static_cast<uint8_t>(value >> (8 * i));
}

// A JPEG2K bayer frame payload: header, then the four channel spans (channel c's codestream `channels[c]`), each followed by
// `gap` filler bytes, then `trailing`. The spans are declared exactly; the gaps and the trailing bytes are inside frame_size
// but outside every span.
std::vector<uint8_t> j2kPayload(const std::vector<std::vector<uint8_t>> & channels, uint8_t gapFill, size_t gap,
                                const std::vector<uint8_t> & trailing)
{
    ASSERT_EQ(static_cast<size_t>(4), channels.size());
    std::vector<uint8_t> out(36, 0);
    putLe32(out, 0, 1u);
    for (int c = 0; c < 4; ++c)
    {
        putLe32(out, 4 + 8 * c, static_cast<uint32_t>(out.size()));
        putLe32(out, 8 + 8 * c, static_cast<uint32_t>(channels[c].size()));
        out.insert(out.end(), channels[c].begin(), channels[c].end());
        out.insert(out.end(), gap, gapFill);
    }
    out.insert(out.end(), trailing.begin(), trailing.end());
    return out;
}

std::vector<uint8_t> j2kPayload(const std::vector<uint8_t> & channel, uint8_t gapFill, size_t gap,
                                const std::vector<uint8_t> & trailing)
{
    return j2kPayload({channel, channel, channel, channel}, gapFill, gap, trailing);
}

// The JPEG2K frame `payload` as frame 0 of the open clip: the object's videoClass, RAWI geometry (64x64), frame 0's index entry
// and chunk file are swapped for the duration of one getMlvFrameSourceHash64 call (the public entry the capture writer calls),
// then restored.
int hashAsClipFrame(MlvPipelineFixture & fixture, const std::vector<uint8_t> & payload, uint64_t * hash)
{
    mlvObject_t * video = fixture.video();
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QByteArray path = QFile::encodeName(dir.filePath(QStringLiteral("frame.j2k")));
    FILE * file = std::fopen(path.constData(), "w+b");
    ASSERT_TRUE(file != nullptr);
    // the frame does not start at offset 0, and its VIDF header (r11: read first, as the display reads it) sits before it
    const std::vector<uint8_t> lead(sizeof(mlv_vidf_hdr_t) + 17, 0x5A);
    ASSERT_EQ(lead.size(), std::fwrite(lead.data(), 1, lead.size(), file));
    ASSERT_EQ(payload.size(), std::fwrite(payload.data(), 1, payload.size(), file));
    ASSERT_EQ(0, std::fflush(file));
    frame_index_t * entry = &video->video_index[0];
    const int chunk = static_cast<int>(entry->chunk_num);
    FILE * const savedFile = video->file[chunk];
    const frame_index_t savedEntry = *entry;
    const uint32_t savedClass = video->MLVI.videoClass;
    const auto savedXRes = video->RAWI.xRes;
    const auto savedYRes = video->RAWI.yRes;
    video->file[chunk] = file;
    entry->block_offset = 0;
    entry->frame_offset = lead.size();
    entry->frame_size = static_cast<uint32_t>(payload.size());
    video->MLVI.videoClass = MLV_VIDEO_CLASS_FLAG_JPEG2K;
    video->RAWI.xRes = kJ2kFrameSide;
    video->RAWI.yRes = kJ2kFrameSide;
    const int status = getMlvFrameSourceHash64(video, 0, hash);
    video->RAWI.xRes = savedXRes;
    video->RAWI.yRes = savedYRes;
    video->MLVI.videoClass = savedClass;
    *entry = savedEntry;
    video->file[chunk] = savedFile;
    std::fclose(file);
    return status;
}

} // namespace

// r9 (r8 adversary pass 2 A1 = Dot B2): the finding's input. A real codestream C and its twin C' that differs ONLY in the final
// byte of the encoder's main-header COM comment (equal length). The real decoder gives both the same samples, so the frame whose
// channel 3 is C' is the frame whose channel 3 is C: one id. On e139944c the span bytes were hashed: two ids.
TEST(VisualCaptureSourceHash, Jpeg2kACommentOpenJphSkipsIsNotTheIdentity)
{
    OjphRealCodecScope realCodec;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    const std::vector<int32_t> picture = j2kQuarterPicture(2048, 12288);
    const std::vector<uint8_t> c = encodeJ2kQuarter(picture);
    const std::vector<uint8_t> twin = j2kComTwin(c);
    ASSERT_EQ(c.size(), twin.size());
    ASSERT_TRUE(c != twin);
    // decoder equivalence, shown by the decoder: equal samples, and they are the encoded picture (lossless)
    ASSERT_TRUE(decodeJ2kQuarter(c) == decodeJ2kQuarter(twin));
    ASSERT_TRUE(decodeJ2kQuarter(c) == picture);
    uint64_t plain = 0;
    uint64_t commented = 0;
    ASSERT_EQ(0, hashAsClipFrame(fixture, j2kPayload({c, c, c, c}, 0, 0, {}), &plain));
    ASSERT_EQ(0, hashAsClipFrame(fixture, j2kPayload({c, c, c, twin}, 0, 0, {}), &commented));
    ASSERT_TRUE(plain == commented);
    uint64_t allCommented = 0;   // every channel's comment changed
    ASSERT_EQ(0, hashAsClipFrame(fixture, j2kPayload(twin, 0, 0, {}), &allCommented));
    ASSERT_TRUE(plain == allCommented);
    // the in-memory entry agrees
    const std::vector<uint8_t> a = j2kPayload({c, c, c, c}, 0, 0, {});
    const std::vector<uint8_t> b = j2kPayload({c, c, c, twin}, 0, 0, {});
    uint64_t ha = 0;
    uint64_t hb = 0;
    ASSERT_EQ(0, mlvJpeg2kFrameSourceHash64(a.data(), a.size(), kJ2kFrameSide, kJ2kFrameSide, &ha));
    ASSERT_EQ(0, mlvJpeg2kFrameSourceHash64(b.data(), b.size(), kJ2kFrameSide, kJ2kFrameSide, &hb));
    ASSERT_TRUE(ha == hb);
    ASSERT_TRUE(ha == plain);
}

// r9 control (rewrites r8's Jpeg2kAChangedSpanByteStillHashesDifferently, which pinned span-bytes hashing: a byte edited in a span
// is not a changed picture when OpenJPH skips it). A different picture: ONE sample changed, in one channel, re-encoded.
TEST(VisualCaptureSourceHash, Jpeg2kOneChangedSampleChangesTheIdentity)
{
    OjphRealCodecScope realCodec;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    const std::vector<int32_t> picture = j2kQuarterPicture(2048, 12288);
    std::vector<int32_t> oneSampleChanged = picture;
    oneSampleChanged[5 * kJ2kQuarter + 7] += 1;
    const std::vector<uint8_t> c = encodeJ2kQuarter(picture);
    const std::vector<uint8_t> changed = encodeJ2kQuarter(oneSampleChanged);
    ASSERT_TRUE(decodeJ2kQuarter(changed) == oneSampleChanged);
    uint64_t plain = 0;
    uint64_t lastChannel = 0;
    uint64_t firstChannel = 0;
    ASSERT_EQ(0, hashAsClipFrame(fixture, j2kPayload({c, c, c, c}, 0, 0, {}), &plain));
    ASSERT_EQ(0, hashAsClipFrame(fixture, j2kPayload({c, c, c, changed}, 0, 0, {}), &lastChannel));
    ASSERT_EQ(0, hashAsClipFrame(fixture, j2kPayload({changed, c, c, c}, 0, 0, {}), &firstChannel));
    ASSERT_TRUE(plain != lastChannel);
    ASSERT_TRUE(plain != firstChannel);
    ASSERT_TRUE(lastChannel != firstChannel);   // which channel holds the change is part of the picture
}

// r8 (kept; real codestreams since r9): bytes outside every declared span (trailing, or between spans) are not the picture.
TEST(VisualCaptureSourceHash, Jpeg2kBytesOutsideTheDeclaredSpansAreNotHashed)
{
    OjphRealCodecScope realCodec;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    const std::vector<uint8_t> c = encodeJ2kQuarter(j2kQuarterPicture(2048, 12288));
    uint64_t trailing00 = 0;
    uint64_t trailing01 = 0;
    ASSERT_EQ(0, hashAsClipFrame(fixture, j2kPayload(c, 0, 0, {0x00}), &trailing00));
    ASSERT_EQ(0, hashAsClipFrame(fixture, j2kPayload(c, 0, 0, {0x01}), &trailing01));
    ASSERT_TRUE(trailing00 == trailing01);   // the r7 key's input: one trailing unused byte
    // the same for bytes between two spans
    uint64_t gap00 = 0;
    uint64_t gapA5 = 0;
    ASSERT_EQ(0, hashAsClipFrame(fixture, j2kPayload(c, 0x00, 3, {}), &gap00));
    ASSERT_EQ(0, hashAsClipFrame(fixture, j2kPayload(c, 0xA5, 3, {}), &gapA5));
    ASSERT_TRUE(gap00 == gapA5);
    ASSERT_TRUE(trailing00 == gap00);
}

// r8 pass 1 A2 on d9836349 (kept; real codestreams since r9): where a span lies is not the picture. Spans in reversed physical
// order, and channels sharing one span, are the picture of the sequential layout.
TEST(VisualCaptureSourceHash, Jpeg2kWhereASpanLiesIsNotHashed)
{
    OjphRealCodecScope realCodec;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    const std::vector<uint8_t> c = encodeJ2kQuarter(j2kQuarterPicture(2048, 12288));
    const std::vector<uint8_t> sequential = j2kPayload(c, 0, 0, {});
    const uint32_t n = static_cast<uint32_t>(c.size());
    std::vector<uint8_t> reversed = sequential;   // channel c's span placed where channel 3 - c's was
    for (int ch = 0; ch < 4; ++ch) putLe32(reversed, 4 + 8 * ch, 36u + static_cast<uint32_t>(3 - ch) * n);
    std::vector<uint8_t> shared(36, 0);           // every channel reads the one span at 36; the frame is header + one span
    putLe32(shared, 0, 1u);
    for (int ch = 0; ch < 4; ++ch) { putLe32(shared, 4 + 8 * ch, 36u); putLe32(shared, 8 + 8 * ch, n); }
    shared.insert(shared.end(), c.begin(), c.end());
    uint64_t hSequential = 0;
    uint64_t hReversed = 0;
    uint64_t hShared = 0;
    ASSERT_EQ(0, hashAsClipFrame(fixture, sequential, &hSequential));
    ASSERT_EQ(0, hashAsClipFrame(fixture, reversed, &hReversed));
    ASSERT_EQ(0, hashAsClipFrame(fixture, shared, &hShared));
    ASSERT_TRUE(hSequential == hReversed);
    ASSERT_TRUE(hSequential == hShared);
}

// r9 (rewrites r8's Jpeg2kTheInMemoryHashIsTheHeaderPlusTheDeclaredSpans): the in-memory identity is FNV-1a 64 over the decoded
// samples as the display stores them (uint16, 2 bytes LE), channel 0..3, row-major -- computed here independently from the
// pictures that were encoded (lossless). Four different channel pictures pin the channel order.
TEST(VisualCaptureSourceHash, Jpeg2kTheInMemoryHashIsTheDecodedPicture)
{
    OjphRealCodecScope realCodec;
    std::vector<std::vector<int32_t>> pictures;
    std::vector<std::vector<uint8_t>> channels;
    for (int ch = 0; ch < 4; ++ch)
    {
        pictures.push_back(j2kQuarterPicture(1000 + 100 * ch, 9000 - 700 * ch));
        channels.push_back(encodeJ2kQuarter(pictures.back()));
    }
    const std::vector<uint8_t> a = j2kPayload(channels, 0x00, 2, {0x00, 0x00});
    const std::vector<uint8_t> b = j2kPayload(channels, 0xEE, 2, {0x7F, 0x80});
    uint64_t ha = 0;
    uint64_t hb = 0;
    ASSERT_EQ(0, mlvJpeg2kFrameSourceHash64(a.data(), a.size(), kJ2kFrameSide, kJ2kFrameSide, &ha));
    ASSERT_EQ(0, mlvJpeg2kFrameSourceHash64(b.data(), b.size(), kJ2kFrameSide, kJ2kFrameSide, &hb));
    ASSERT_TRUE(ha == hb);
    uint64_t expected = 0xcbf29ce484222325ull;
    for (const std::vector<int32_t> & picture : pictures)
        for (int32_t value : picture)
        {
            const uint8_t sample[2] = {static_cast<uint8_t>(value & 0xFF), static_cast<uint8_t>((value >> 8) & 0xFF)};
            for (uint8_t byte : sample) { expected ^= byte; expected *= 0x100000001b3ull; }
        }
    ASSERT_TRUE(ha == expected);
    // channel order is part of it: channels 0 and 1 swapped is another picture
    const std::vector<uint8_t> swapped = j2kPayload({channels[1], channels[0], channels[2], channels[3]}, 0, 0, {});
    uint64_t hs = 0;
    ASSERT_EQ(0, mlvJpeg2kFrameSourceHash64(swapped.data(), swapped.size(), kJ2kFrameSide, kJ2kFrameSide, &hs));
    ASSERT_TRUE(hs != ha);
    // what the display decode refuses has no identity, and the hash is never written
    uint64_t hash = 0x77;
    std::vector<uint8_t> badVersion = a;
    badVersion[0] = 2;
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(badVersion.data(), badVersion.size(), kJ2kFrameSide, kJ2kFrameSide, &hash));
    std::vector<uint8_t> spanPastEnd = a;
    putLe32(spanPastEnd, 32, static_cast<uint32_t>(a.size()));   // channel 3's size runs past frame_size
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(spanPastEnd.data(), spanPastEnd.size(), kJ2kFrameSide, kJ2kFrameSide, &hash));
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(a.data(), 35, kJ2kFrameSide, kJ2kFrameSide, &hash));   // shorter than the header
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(a.data(), a.size(), 63, kJ2kFrameSide, &hash));       // odd geometry
    // the codestreams are 32x32: RAWI 128x128 asks for 64x64 quarters, which the display's probe check refuses
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(a.data(), a.size(), 2 * kJ2kFrameSide, 2 * kJ2kFrameSide, &hash));
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(nullptr, a.size(), kJ2kFrameSide, kJ2kFrameSide, &hash));
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(a.data(), a.size(), kJ2kFrameSide, kJ2kFrameSide, nullptr));
    ASSERT_TRUE(hash == 0x77);
}

// r9 (b): fail closed. A frame whose decode the codec refuses has no identity -- never a hash of its bytes. The stub codec
// (no OjphRealCodecScope) stands in for a probe that fails and a decode that returns short; the layout is valid, so on
// e139944c (which never decoded) both still got an id.
TEST(VisualCaptureSourceHash, Jpeg2kAFrameTheDecoderRefusesHasNoIdentity)
{
    const std::vector<uint8_t> codestream = {0xFF, 0x4F, 0xFF, 0x51, 0x00, 0x2F, 0x00, 0x00, 0x11, 0x22, 0x33, 0x44};
    const std::vector<uint8_t> frame = j2kPayload(codestream, 0, 0, {});
    struct RestoreStubPlan { ~RestoreStubPlan() { ojphStubReset(OjphStubPlan()); } } restore;
    uint64_t hash = 0x77;
    OjphStubPlan probeFails = ojphStubQuarterFramePlan(kJ2kQuarter, kJ2kQuarter);
    probeFails.probe_result = -1;
    ojphStubReset(probeFails);
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(frame.data(), frame.size(), kJ2kFrameSide, kJ2kFrameSide, &hash));
    ASSERT_TRUE(ojphStubProbeCount() > 0);
    OjphStubPlan decodeShort = ojphStubQuarterFramePlan(kJ2kQuarter, kJ2kQuarter);
    decodeShort.decode_pixels = kJ2kQuarter * kJ2kQuarter - 1;
    ojphStubReset(decodeShort);
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(frame.data(), frame.size(), kJ2kFrameSide, kJ2kFrameSide, &hash));
    ASSERT_TRUE(ojphStubDecodeCount() > 0);
    OjphStubPlan wrongGeometry = ojphStubQuarterFramePlan(kJ2kQuarter / 2, kJ2kQuarter);
    ojphStubReset(wrongGeometry);
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(frame.data(), frame.size(), kJ2kFrameSide, kJ2kFrameSide, &hash));
    ASSERT_TRUE(hash == 0x77);
}

// r10 (adversary r9 pass 3 A2 on 1c438b0c): the json's input -- OjphRealCodecScope outer; { OjphRealCodecScope inner; } and then a
// decode -- got the STUB, because the inner scope's destructor installed the default plan while the outer scope was alive. Each
// scope now restores the plan it replaced. The default stub plan probes 0 x 0, so a real codestream through the stub is refused.
TEST(VisualCaptureSourceHash, Jpeg2kANestedRealCodecScopeLeavesTheOuterOnTheRealCodec)
{
    const std::vector<uint8_t> frame = j2kPayload(encodeJ2kQuarter(j2kQuarterPicture(2048, 12288)), 0, 0, {});
    uint64_t real = 0;
    {
        OjphRealCodecScope outer;
        ASSERT_EQ(0, mlvJpeg2kFrameSourceHash64(frame.data(), frame.size(), kJ2kFrameSide, kJ2kFrameSide, &real));
        {
            OjphRealCodecScope inner;
            uint64_t nested = 0;
            ASSERT_EQ(0, mlvJpeg2kFrameSourceHash64(frame.data(), frame.size(), kJ2kFrameSide, kJ2kFrameSide, &nested));
            ASSERT_TRUE(nested == real);
        }
        uint64_t afterInner = 0;   // 1c438b0c: the stub again, so this frame was refused
        ASSERT_EQ(0, mlvJpeg2kFrameSourceHash64(frame.data(), frame.size(), kJ2kFrameSide, kJ2kFrameSide, &afterInner));
        ASSERT_TRUE(afterInner == real);
    }
    // the outermost scope restores the stub
    uint64_t stub = 0x77;
    ASSERT_NE(0, mlvJpeg2kFrameSourceHash64(frame.data(), frame.size(), kJ2kFrameSide, kJ2kFrameSide, &stub));
    ASSERT_TRUE(stub == 0x77);
}

// r10 (adversary r10 pass 1 A1 on 5f23fb9d): the display applies the clip's CURV linearisation table to every decoded sample
// (getMlvRawFrameUint16Direct: unpackedFrame[i] = video->linearise_lut[unpackedFrame[i]]), but the identity hashed the samples
// BEFORE it. The json's input: RAWI 64x64 at 14 bits, a CURV table LUT[i] = floor(i / 2), and LJ92 frames encodeLj92(1000, 9000)
// and encodeLj92(1001, 9000): the display shows 500 | 4500 for both, yet 5f23fb9d gave them two ids ("differ" over a static
// picture, FROZEN_DISPLAY counted=false). The identity is now taken after the table, on every frame class.
namespace
{

// `payload` as frame 0 of the open clip, with videoClass `videoClass`, RAWI 64x64 at `bitsPerPixel` and the CURV table `lut`
// (nullptr: none), for one getMlvFrameSourceHash64 call; everything is restored after it.
// r11: headerBytesBeforeEnd >= 0 puts the index entry's block_offset that many bytes before the end of the chunk file (0: at its
// size, the adversary's input); -1 puts it at offset 0, a whole header before the frame. displayStatus, when given, is the display
// reader's answer for the same entry, asked only where it refuses at the header (it stops before any decode state).
int hashAsClipFrameWithCurve(MlvPipelineFixture & fixture, const std::vector<uint8_t> & payload, uint32_t videoClass,
                             int bitsPerPixel, uint16_t * lut, uint64_t * hash,
                             int headerBytesBeforeEnd = -1, int * displayStatus = nullptr)
{
    mlvObject_t * video = fixture.video();
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QByteArray path = QFile::encodeName(dir.filePath(QStringLiteral("frame.bin")));
    FILE * file = std::fopen(path.constData(), "w+b");
    ASSERT_TRUE(file != nullptr);
    const std::vector<uint8_t> lead(sizeof(mlv_vidf_hdr_t) + 17, 0x5A);
    ASSERT_EQ(lead.size(), std::fwrite(lead.data(), 1, lead.size(), file));
    ASSERT_EQ(payload.size(), std::fwrite(payload.data(), 1, payload.size(), file));
    ASSERT_EQ(0, std::fflush(file));
    frame_index_t * entry = &video->video_index[0];
    const int chunk = static_cast<int>(entry->chunk_num);
    FILE * const savedFile = video->file[chunk];
    const frame_index_t savedEntry = *entry;
    const uint32_t savedClass = video->MLVI.videoClass;
    const auto savedXRes = video->RAWI.xRes;
    const auto savedYRes = video->RAWI.yRes;
    const auto savedBits = video->RAWI.raw_info.bits_per_pixel;
    uint16_t * const savedLut = video->linearise_lut;
    video->file[chunk] = file;
    entry->block_offset = headerBytesBeforeEnd < 0
        ? 0 : static_cast<uint64_t>(lead.size() + payload.size()) - static_cast<uint64_t>(headerBytesBeforeEnd);
    entry->frame_offset = lead.size();
    entry->frame_size = static_cast<uint32_t>(payload.size());
    video->MLVI.videoClass = videoClass;
    video->RAWI.xRes = kJ2kFrameSide;
    video->RAWI.yRes = kJ2kFrameSide;
    video->RAWI.raw_info.bits_per_pixel = bitsPerPixel;
    video->linearise_lut = lut;
    const int status = getMlvFrameSourceHash64(video, 0, hash);
    if (displayStatus) {
        std::vector<uint16_t> shown(static_cast<size_t>(kJ2kFrameSide) * kJ2kFrameSide);
        const mlv_vidf_hdr_t savedVidf = video->VIDF;
        *displayStatus = getMlvRawFrameUint16Uncached(video, 0, shown.data());
        video->VIDF = savedVidf;
    }
    video->linearise_lut = savedLut;
    video->RAWI.raw_info.bits_per_pixel = savedBits;
    video->RAWI.xRes = savedXRes;
    video->RAWI.yRes = savedYRes;
    video->MLVI.videoClass = savedClass;
    *entry = savedEntry;
    video->file[chunk] = savedFile;
    std::fclose(file);
    return status;
}

std::vector<uint16_t> halvingCurve()
{
    std::vector<uint16_t> lut(MLV_LINEARISE_LUT_ENTRIES);
    for (size_t i = 0; i < lut.size(); ++i) lut[i] = static_cast<uint16_t>(i / 2);
    return lut;
}

uint32_t rawVideoClass(const MlvPipelineFixture & fixture)
{
    return const_cast<MlvPipelineFixture &>(fixture).video()->MLVI.videoClass
        & ~static_cast<uint32_t>(MLV_VIDEO_CLASS_FLAG_LJ92 | MLV_VIDEO_CLASS_FLAG_JPEG2K);
}

} // namespace

TEST(VisualCaptureSourceHash, Lj92TwoFramesTheCurveShowsAsOnePictureShareTheIdentity)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    std::vector<uint16_t> curve = halvingCurve();
    const uint32_t lj92Class = rawVideoClass(fixture) | MLV_VIDEO_CLASS_FLAG_LJ92;
    const std::vector<uint8_t> a = encodeLj92(1000, 9000);
    const std::vector<uint8_t> b = encodeLj92(1001, 9000);
    const std::vector<uint8_t> c = encodeLj92(1000, 9002);   // 500 | 4501 on display: a different picture
    ASSERT_FALSE(a.empty() || b.empty() || c.empty());
    uint64_t ha = 0, hb = 0, hc = 0;
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, a, lj92Class, 14, curve.data(), &ha));
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, b, lj92Class, 14, curve.data(), &hb));
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, c, lj92Class, 14, curve.data(), &hc));
    ASSERT_TRUE(ha == hb);   // 5f23fb9d: b47aad9ecdade325 vs 2caa1a2fe210c325
    ASSERT_TRUE(ha != hc);
    // control: without a CURV table the display shows 1000 vs 1001, two pictures
    uint64_t na = 0, nb = 0;
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, a, lj92Class, 14, nullptr, &na));
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, b, lj92Class, 14, nullptr, &nb));
    ASSERT_TRUE(na != nb);
    ASSERT_TRUE(na != ha);
}

TEST(VisualCaptureSourceHash, Jpeg2kTwoFramesTheCurveShowsAsOnePictureShareTheIdentity)
{
    OjphRealCodecScope realCodec;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    std::vector<uint16_t> curve = halvingCurve();
    const uint32_t j2kClass = rawVideoClass(fixture) | MLV_VIDEO_CLASS_FLAG_JPEG2K;
    const std::vector<uint8_t> a = j2kPayload(encodeJ2kQuarter(j2kQuarterPicture(2048, 12288)), 0, 0, {});
    const std::vector<uint8_t> b = j2kPayload(encodeJ2kQuarter(j2kQuarterPicture(2049, 12288)), 0, 0, {});
    uint64_t ha = 0, hb = 0, na = 0, nb = 0;
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, a, j2kClass, 14, curve.data(), &ha));
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, b, j2kClass, 14, curve.data(), &hb));
    ASSERT_TRUE(ha == hb);   // both display 1024 | 6144
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, a, j2kClass, 14, nullptr, &na));
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, b, j2kClass, 14, nullptr, &nb));
    ASSERT_TRUE(na != nb);
}

// Uncompressed RAW: a CURV table that maps every sample to one value shows any two frames as one picture; without it, two
// different packed frames are two pictures.
TEST(VisualCaptureSourceHash, RawTwoFramesTheCurveShowsAsOnePictureShareTheIdentity)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    std::vector<uint16_t> flat(MLV_LINEARISE_LUT_ENTRIES, 777);
    const uint32_t rawClass = rawVideoClass(fixture);
    const size_t packed = static_cast<size_t>(kJ2kFrameSide) * kJ2kFrameSide * 14 / 8;
    const std::vector<uint8_t> a(packed, 0x00);
    std::vector<uint8_t> b(packed, 0x00);
    for (size_t i = 0; i < packed; i += 7) b[i] = 0x5A;
    uint64_t ha = 0, hb = 0, na = 0, nb = 0;
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, a, rawClass, 14, flat.data(), &ha));
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, b, rawClass, 14, flat.data(), &hb));
    ASSERT_TRUE(ha == hb);
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, a, rawClass, 14, nullptr, &na));
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, b, rawClass, 14, nullptr, &nb));
    ASSERT_TRUE(na != nb);
}

// r11 (adversary r11 pass 1 A1 on 9ae728fc): the display reads a frame's VIDF header at block_offset before its payload and refuses
// the frame when that read fails; the identity skipped the read, so a frame the display refuses got an id. The json's input: an entry
// whose block_offset is the chunk file's size N, its payload valid. 9ae728fc returned 0 for every class (harness: 20 of 30).
namespace
{

void assertAFrameWithNoReadableHeaderHasNoIdentity(MlvPipelineFixture & fixture, const std::vector<uint8_t> & payload,
                                                   uint32_t videoClass, uint16_t * lut)
{
    ASSERT_FALSE(payload.empty());
    uint64_t reference = 0;
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, payload, videoClass, 14, lut, &reference));
    const int headerSize = static_cast<int>(sizeof(mlv_vidf_hdr_t));
    for (const int before : { 0, headerSize - 1 }) {   // the json's input; a partial header
        uint64_t hash = reference;
        int display = 0;
        ASSERT_NE(0, hashAsClipFrameWithCurve(fixture, payload, videoClass, 14, lut, &hash, before, &display));
        ASSERT_NE(0, display);
    }
    // control: a whole header ending exactly at the end of the file is readable, so the frame keeps its one identity
    uint64_t whole = 0;
    ASSERT_EQ(0, hashAsClipFrameWithCurve(fixture, payload, videoClass, 14, lut, &whole, headerSize));
    ASSERT_TRUE(whole == reference);
}

} // namespace

TEST(VisualCaptureSourceHash, Lj92AFrameWithNoReadableVidfHeaderHasNoIdentity)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    std::vector<uint16_t> curve = halvingCurve();
    const uint32_t lj92Class = rawVideoClass(fixture) | MLV_VIDEO_CLASS_FLAG_LJ92;
    const std::vector<uint8_t> frame = encodeLj92(1000, 9000);
    assertAFrameWithNoReadableHeaderHasNoIdentity(fixture, frame, lj92Class, nullptr);
    assertAFrameWithNoReadableHeaderHasNoIdentity(fixture, frame, lj92Class, curve.data());
}

TEST(VisualCaptureSourceHash, Jpeg2kAFrameWithNoReadableVidfHeaderHasNoIdentity)
{
    OjphRealCodecScope realCodec;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    const uint32_t j2kClass = rawVideoClass(fixture) | MLV_VIDEO_CLASS_FLAG_JPEG2K;
    assertAFrameWithNoReadableHeaderHasNoIdentity(fixture, j2kPayload(encodeJ2kQuarter(j2kQuarterPicture(2048, 12288)), 0, 0, {}),
                                                  j2kClass, nullptr);
}

TEST(VisualCaptureSourceHash, RawAFrameWithNoReadableVidfHeaderHasNoIdentity)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    std::vector<uint16_t> flat(MLV_LINEARISE_LUT_ENTRIES, 777);
    const uint32_t rawClass = rawVideoClass(fixture);
    std::vector<uint8_t> frame(static_cast<size_t>(kJ2kFrameSide) * kJ2kFrameSide * 14 / 8, 0x00);
    for (size_t i = 0; i < frame.size(); i += 7) frame[i] = 0x5A;
    assertAFrameWithNoReadableHeaderHasNoIdentity(fixture, frame, rawClass, nullptr);
    assertAFrameWithNoReadableHeaderHasNoIdentity(fixture, frame, rawClass, flat.data());
}

// r10 (Dot B3, lj92.c find()): the marker scan's bounds, through lj92_open (find() is static to lj92.c). Payloads of datalen 0..3
// -- 0xFF as the last byte, FF D8 as the last two bytes, and no marker -- each followed IN MEMORY by a whole valid LJ92 stream, so
// a scan that looked past datalen would find an SOI and open it. Every one is refused, as before r10. The datalen = INT_MAX case
// (where `ix += 2` overflowed) needs a 2 GiB payload, so it is pinned by the white-box -ftrapv harness in the r10 run dir instead.
TEST(VisualCaptureSourceHash, Lj92TheMarkerScanNeverLooksPastThePayloadEnd)
{
    const std::vector<uint8_t> valid = encodeLj92(1000, 9000);
    ASSERT_TRUE(valid.size() > 4);
    ASSERT_EQ(LJ92_ERROR_NONE, lj92OpenStatus(valid, 0));   // control: the stream alone opens
    for (int len = 0; len <= 3; ++len)
        for (int shape = 0; shape < 3; ++shape)
        {
            std::vector<uint8_t> buffer(static_cast<size_t>(len), 0x40);
            if (shape == 0 && len >= 1) buffer[len - 1] = 0xFF;
            if (shape == 1 && len >= 2) { buffer[len - 2] = 0xFF; buffer[len - 1] = 0xD8; }
            buffer.insert(buffer.end(), valid.begin(), valid.end());
            lj92 decoder = nullptr;
            int width = 0, height = 0, bitdepth = 0, components = 0;
            const int ret = lj92_open(&decoder, buffer.data(), len, &width, &height, &bitdepth, &components);
            if (ret == LJ92_ERROR_NONE) lj92_close(decoder);
            ASSERT_EQ(LJ92_ERROR_CORRUPT, ret);
        }
    // control: an in-payload decode is the encoded picture (lossless), as it was before r10
    std::vector<uint16_t> decoded;
    ASSERT_TRUE(decodeLj92(valid, &decoded));
    ASSERT_EQ(static_cast<size_t>(64 * 64), decoded.size());
    for (int i = 0; i < 64 * 64; ++i) ASSERT_EQ((i % 64) < 32 ? 1000 : 9000, static_cast<int>(decoded[i]));
}
