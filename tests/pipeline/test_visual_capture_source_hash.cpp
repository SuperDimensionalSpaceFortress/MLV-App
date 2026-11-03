// PLAYBACK-SOURCE-FRAME-IDENTITY-1: pins getMlvFrameSourceHash64, the source-frame identity the
// --visual-capture-dir writer puts in visual.json (source_id per play frame, and the
// source_difference verdict across them). It hashes the bytes the decoder reads for a frame
// (the encoded VIDF payload at video_index[frame].frame_offset; r5: for LJ92, the picture liblj92 decodes from it), so it must be
// stable for one frame, differ between two frames of a moving clip, and refuse (nonzero, no
// identity) every input it cannot answer for, which makes the writer omit the field.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"

#include <cstdint>
#include <cstdlib>
#include <initializer_list>
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
