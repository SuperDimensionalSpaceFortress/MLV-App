// PLAYBACK-SOURCE-FRAME-IDENTITY-1: pins getMlvFrameSourceHash64, the source-frame identity the
// --visual-capture-dir writer puts in visual.json (source_id per play frame, and the
// source_difference verdict across them). It hashes the bytes the decoder reads for a frame
// (the encoded VIDF payload at video_index[frame].frame_offset) without decoding, so it must be
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
    lj92 decoder;
    int width = 0, height = 0, bitdepth = 0, components = 0;
    if (lj92_open(&decoder, payload.data(), static_cast<int>(payload.size()),
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
// the hash never stops at an EOI: every byte after the SOS header counts. A picture that differs only after an FF D9 in
// the entropy data keeps a different identity ("identical" is never false); unread bytes after the scan read "differ"
// (the loud direction -- a FROZEN_DISPLAY a person looks at, never a frozen display waived as a static clip).
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
    int status = -1;
    const uint64_t firstHash = lj92Hash(first, &status);
    ASSERT_EQ(0, status);
    const uint64_t secondHash = lj92Hash(second, &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(firstHash != secondHash);

    // Bytes after the scan's EOI are hashed too: conservative, never "identical" for bytes that might be read.
    const std::vector<uint8_t> plain = encodeLj92(2048, 12288);
    std::vector<uint8_t> padded = plain;
    padded.push_back(0x00);
    padded.push_back(0x7F);
    const uint64_t plainHash = lj92Hash(plain, &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(lj92Hash(padded, &status) != plainHash);
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
    int status = -1;
    const uint64_t first = lj92Hash(build(0x00), &status);
    ASSERT_EQ(0, status);
    const uint64_t second = lj92Hash(build(0xC0), &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(first != second);
}

// r9 A2: a DHT value past sum(counts) is never read, so two payloads that differ only there are the same picture.
TEST(VisualCaptureSourceHash, Lj92ADhtValueBeyondTheDeclaredSymbolsIsNotHashed)
{
    auto build = [&](uint8_t unused)
    {
        return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x16, 0x00, 0x02 }, zeros(15), { 0x00, 0x01, unused },
                       kSof3, kSos, zeros(512), { 0xFF, 0xD9 } });
    };
    int status = -1;
    const uint64_t first = lj92Hash(build(0x41), &status);
    ASSERT_EQ(0, status);
    const uint64_t second = lj92Hash(build(0x42), &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(first == second);
    // ...while a DECLARED symbol still counts.
    auto declared = [&](uint8_t value)
    {
        return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x16, 0x00, 0x02 }, zeros(15), { 0x00, value, 0x41 },
                       kSof3, kSos, zeros(512), { 0xFF, 0xD9 } });
    };
    ASSERT_TRUE(lj92Hash(declared(0x01), &status) != lj92Hash(declared(0x02), &status));
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
    int status = -1;
    const uint64_t first = lj92Hash(build(0x41), &status);
    ASSERT_EQ(0, status);
    const uint64_t second = lj92Hash(build(0x42), &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(first == second);
    // The second symbol of the first level IS read (LUT entries 2 and 3).
    auto second_symbol = [&](uint8_t value)
    {
        return bytes({ { 0xFF, 0xD8, 0xFF, 0xC4, 0x00, 0x16, 0x00, 0x02, 0x01 }, zeros(14), { 0x00, value, 0x41 },
                       kSof3, kSos, zeros(64), { 0xFF, 0xD9 } });
    };
    ASSERT_TRUE(lj92Hash(second_symbol(0x0D), &status) != lj92Hash(second_symbol(0x0E), &status));
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
    lj92Hash(withSegmentAfterSoi(plain, {0xFF, 0xFE, 0x00, 0x01}), &status); // a segment length under 2
    ASSERT_NE(0, status);
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
