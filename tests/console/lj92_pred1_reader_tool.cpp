// LJ92-PRED1-FAST-PATH-1: equality tests, microbenchmark and worker-scaling tool for the predictor-1 fast path's bit
// reader. One binary holds both readers (lj92_set_pred1_legacy_reader forces the legacy 16-bit-pair reader per calling
// thread), so every comparison runs on the same bytes in the same process.
//
//   lj92_pred1_reader_tool --selftest [--seed N] [--root DIR]
//       Decodes with the legacy and the default reader and requires identical return codes and identical samples:
//       every lossless frame of the in-tree fixtures, lj92-conformant streams this tool encodes itself (1-4 components,
//       several Huffman tables including SSSS 15 / 16 and a 16-bit code length), random entropy, 0xFF-dense entropy,
//       planted 0xFF / marker bytes, bit flips, and truncations. Also requires the u64 reader to be TAKEN on every
//       eligible layout and NOT taken when the table names an SSSS above 16.
//   lj92_pred1_reader_tool --bench [--iters N] [--root DIR]
//       Interleaved legacy/new single-thread decode of every fixture frame; min / median / p95 ms per frame.
//   lj92_pred1_reader_tool --scale [--seconds S] [--root DIR]
//       RAM-resident payload copies, one decoder and one output buffer per worker, 1/2/4/8 workers: aggregate MP/s and
//       per-frame decode latency p50/p95 for the legacy and the default reader.
//
// Options are strict: --iters 1..100000, --seed 0..4294967295, --seconds 0 < S <= 3600; a bad or missing value or an unknown
// option exits 2. --bench / --scale exit 3 (no BENCH_RESULT / SCALE_RESULT) when a decode fails, when an arm left the
// predictor-1 fast path, or when the new arm did not run the u64 reader.
//
// Fixtures are named as ONE complete repo-relative string and found by walking up from the working directory (or --root).
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <functional>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

extern "C" {
#include "../../src/mlv/liblj92/lj92.h"
}

namespace {

typedef std::vector<uint8_t> Bytes;

const char * const kFixtures[] = {
    "tests/fixtures/clips/tiny_dual_iso.mlv",
    "tests/fixtures/clips/large_dual_iso.mlv",
};

// ---------------------------------------------------------------------------------------------------- file helpers

bool file_exists(const std::string & path)
{
    std::ifstream f(path.c_str(), std::ios::binary);
    return f.good();
}

std::string find_root(const std::string & given)
{
    if (!given.empty()) return given;
    std::string dir = ".";
    for (int up = 0; up < 12; up++)
    {
        if (file_exists(dir + "/" + kFixtures[0])) return dir;
        dir += "/..";
    }
    return std::string();
}

bool read_file(const std::string & path, Bytes & out)
{
    std::ifstream f(path.c_str(), std::ios::binary | std::ios::ate);
    if (!f.good()) return false;
    const std::streamoff size = f.tellg();
    f.seekg(0);
    out.resize((size_t)size);
    f.read(reinterpret_cast<char *>(out.data()), size);
    return f.good();
}

uint32_t rd32(const Bytes & b, size_t at) { return (uint32_t)b[at] | ((uint32_t)b[at + 1] << 8) | ((uint32_t)b[at + 2] << 16) | ((uint32_t)b[at + 3] << 24); }

// The VIDF payloads that start with the lossless-JPEG start-of-image marker. Block: type[4] size[4] ts[8] then VIDF:
// frameNumber[4] cropX[2] cropY[2] panX[2] panY[2] frameSpace[4]; the payload follows frameSpace bytes of padding.
void collect_lj92_payloads(const Bytes & file, std::vector<Bytes> & frames)
{
    size_t pos = 0;
    while (pos + 16 <= file.size())
    {
        const uint32_t size = rd32(file, pos + 4);
        if (size < 16 || pos + size > file.size()) break;
        if (std::memcmp(&file[pos], "VIDF", 4) == 0 && size >= 32)
        {
            const uint32_t space = rd32(file, pos + 28);
            const size_t start = pos + 32 + space;
            if (start + 2 < pos + size && file[start] == 0xFF && file[start + 1] == 0xD8)
                frames.emplace_back(file.begin() + (long)start, file.begin() + (long)(pos + size));
        }
        pos += size;
    }
}

// ---------------------------------------------------------------------------------------------------- decode helper

struct DecodeInfo
{
    int ret = -1;
    int open_ret = -1;
    int width = 0, height = 0, bits = 0, components = 0;
    int predictor = -1, scan_components = 0, eligible = 0, fast_active = 0, u64_active = 0, max_ssss = -1;
    std::vector<uint16_t> samples;
};

// lj92_open edits the Huffman head in place, so every decode gets a private copy of the stream.
DecodeInfo decode_copy(const Bytes & stream, bool legacy)
{
    DecodeInfo d;
    Bytes copy(stream);
    lj92 h = nullptr;
    d.open_ret = lj92_open(&h, copy.data(), (int)copy.size(), &d.width, &d.height, &d.bits, &d.components);
    if (d.open_ret != LJ92_ERROR_NONE) return d;
    d.samples.assign((size_t)d.width * (size_t)d.height * (size_t)d.components, 0xA5A5u);
    lj92_set_pred1_legacy_reader(legacy ? 1 : 0);
    d.ret = lj92_decode(h, d.samples.data(), (int)d.samples.size(), 0, nullptr, 0);
    lj92_set_pred1_legacy_reader(0);
    d.predictor = lj92_get_last_predictor();
    d.scan_components = lj92_get_last_scan_component_count();
    d.eligible = lj92_get_last_pred1_fast_path_eligible();
    d.fast_active = lj92_get_last_pred1_fast_path_active();
    d.u64_active = lj92_get_last_pred1_u64_reader_active();
    d.max_ssss = lj92_get_last_huff_max_ssss();
    lj92_close(h);
    return d;
}

// ---------------------------------------------------------------------------------------------------- self-test

struct Counters
{
    int checks = 0, failures = 0, refusals = 0;
    bool quiet = false;   // a probe counter that is MEANT to fail (the oracle self-check) must not print FAIL lines
    void fail(const std::string & what)
    {
        failures++;
        if (!quiet && failures <= 40) std::fprintf(stderr, "FAIL %s\n", what.c_str());
    }
};

// Legacy and default reader must agree exactly; expect_u64 says whether the default decode must have taken the u64 reader.
// r2c (sol r1 S2): a case whose two readers both refuse to open compared nothing. It is a failure unless the caller says
// the case may be refused (a truncated or corrupted stream); a permitted refusal is counted apart from the checks.
void compare_readers(Counters & c, const std::string & label, const Bytes & stream, int expect_u64, const std::vector<uint16_t> * expected = nullptr, bool may_refuse = false)
{
    const DecodeInfo legacy = decode_copy(stream, true);
    const DecodeInfo fresh = decode_copy(stream, false);
    if (legacy.open_ret != fresh.open_ret) { c.checks++; c.fail(label + ": open differs"); return; }
    if (legacy.open_ret != LJ92_ERROR_NONE)
    {
        if (may_refuse) { c.refusals++; return; }
        c.checks++;
        c.fail(label + ": both readers refuse to open a stream this case needs decoded, so nothing was compared");
        return;
    }
    c.checks++;
    if (legacy.ret != fresh.ret) { c.fail(label + ": decode return differs"); return; }
    if (legacy.samples != fresh.samples) { c.fail(label + ": decoded samples differ"); return; }
    if (legacy.u64_active != 0) c.fail(label + ": forced-legacy decode reports the u64 reader");
    if (expect_u64 >= 0 && fresh.u64_active != expect_u64)
        c.fail(label + ": u64 reader taken=" + std::to_string(fresh.u64_active) + " expected=" + std::to_string(expect_u64));
    if (expected != nullptr && fresh.samples != *expected) c.fail(label + ": decode differs from the encoded picture");
}

// r2d (sol r1 S2): a stream that must be refused is asserted as a refusal, explicitly: both readers must refuse to open it.
void expect_refusal(Counters & c, const std::string & label, const Bytes & stream)
{
    const DecodeInfo legacy = decode_copy(stream, true);
    const DecodeInfo fresh = decode_copy(stream, false);
    c.checks++;
    if (legacy.open_ret == LJ92_ERROR_NONE || fresh.open_ret == LJ92_ERROR_NONE)
        c.fail(label + ": a stream this case needs refused was opened (legacy open=" + std::to_string(legacy.open_ret) + " default open=" + std::to_string(fresh.open_ret) + ")");
}

// A canonical Huffman table over a chosen symbol set (SSSS values), code lengths 1..16 with Kraft sum < 1.
struct Table
{
    std::vector<int> symbols;        // symbol value per entry, in DHT order (length ascending, then symbol order)
    std::vector<int> lengths;        // code length per entry
    std::vector<uint32_t> codes;     // canonical code per entry
    int counts[17] = {0};
    int max_len = 0;
};

Table make_table(std::vector<int> symbols, int target_max_len, std::mt19937 & rng)
{
    std::sort(symbols.begin(), symbols.end());
    const int n = (int)symbols.size();
    std::vector<int> len((size_t)n);
    int l0 = 1;
    while ((1 << l0) < n + 1) l0++;
    if (l0 > target_max_len) l0 = target_max_len;
    for (int i = 0; i < n; i++) len[(size_t)i] = l0;
    auto kraft = [&](void) { uint32_t s = 0; for (int i = 0; i < n; i++) s += 1u << (16 - len[(size_t)i]); return s; };
    if (target_max_len > l0) len[(size_t)(rng() % (uint32_t)n)] = target_max_len;
    for (int step = 0; step < 400; step++)
    {
        const int i = (int)(rng() % (uint32_t)n);
        if (len[(size_t)i] <= 1) continue;
        len[(size_t)i]--;
        if (kraft() >= 65536u) len[(size_t)i]++;
    }
    // keep the all-ones code free and make sure one symbol sits at the requested maximum length
    while (kraft() >= 65536u)
    {
        const int i = (int)(rng() % (uint32_t)n);
        if (len[(size_t)i] < 16) len[(size_t)i]++;
    }
    Table t;
    std::vector<int> order((size_t)n);
    for (int i = 0; i < n; i++) order[(size_t)i] = i;
    std::stable_sort(order.begin(), order.end(), [&](int a, int b) { return len[(size_t)a] < len[(size_t)b]; });
    uint32_t code = 0;
    int prev_len = len[(size_t)order[0]];
    for (int k = 0; k < n; k++)
    {
        const int i = order[(size_t)k];
        while (prev_len < len[(size_t)i]) { code <<= 1; prev_len++; }
        t.symbols.push_back(symbols[(size_t)i]);
        t.lengths.push_back(len[(size_t)i]);
        t.codes.push_back(code);
        code++;
        t.counts[len[(size_t)i]]++;
        t.max_len = std::max(t.max_len, len[(size_t)i]);
    }
    return t;
}

void put16(Bytes & b, int v) { b.push_back((uint8_t)(v >> 8)); b.push_back((uint8_t)v); }

Bytes make_header(const Table & t, int width, int height, int bits, int comps)
{
    Bytes b;
    b.push_back(0xFF); b.push_back(0xD8);
    b.push_back(0xFF); b.push_back(0xC4);
    put16(b, 2 + 1 + 16 + (int)t.symbols.size());
    b.push_back(0x00);
    for (int l = 1; l <= 16; l++) b.push_back((uint8_t)t.counts[l]);
    for (size_t i = 0; i < t.symbols.size(); i++) b.push_back((uint8_t)t.symbols[i]);
    b.push_back(0xFF); b.push_back(0xC3);
    put16(b, 8 + 3 * comps);
    b.push_back((uint8_t)bits);
    put16(b, height);
    put16(b, width);
    b.push_back((uint8_t)comps);
    for (int c = 0; c < comps; c++) { b.push_back((uint8_t)(c + 1)); b.push_back(0x11); b.push_back(0x00); }
    b.push_back(0xFF); b.push_back(0xDA);
    put16(b, 6 + 2 * comps);
    b.push_back((uint8_t)comps);
    for (int c = 0; c < comps; c++) { b.push_back((uint8_t)(c + 1)); b.push_back(0x00); }
    b.push_back(0x01); b.push_back(0x00); b.push_back(0x00);   // predictor 1, Se 0, Ah/Al 0
    return b;
}

struct BitWriter
{
    Bytes & out;
    uint64_t acc = 0;
    int n = 0;
    explicit BitWriter(Bytes & o) : out(o) {}
    void put(uint32_t value, int bits)
    {
        for (int i = bits - 1; i >= 0; i--)
        {
            acc = (acc << 1) | ((value >> i) & 1u);
            if (++n == 8) { flush_byte(); }
        }
    }
    void flush_byte()
    {
        const uint8_t byte = (uint8_t)acc;
        out.push_back(byte);
        if (byte == 0xFF) out.push_back(0x00);
        acc = 0; n = 0;
    }
    void finish() { while (n != 0) put(1, 1); }
};

int ssss_of(int diff)           // diff in [-32768, 32768]
{
    if (diff == 0) return 0;
    if (diff == 32768 || diff == -32768) return 16;
    int m = diff < 0 ? -diff : diff;
    int s = 0;
    while (m) { s++; m >>= 1; }
    return s;
}

// Draws a difference whose category is one the table holds (SSSS <= 16 only). scale picks small / large categories.
int draw_diff(const Table & t, std::mt19937 & rng, int scale_mode)
{
    std::vector<int> usable;
    for (size_t i = 0; i < t.symbols.size(); i++) if (t.symbols[i] <= 16) usable.push_back(t.symbols[i]);
    std::sort(usable.begin(), usable.end());
    int s;
    if (scale_mode == 0) { s = usable[rng() % usable.size()]; }                       // uniform over categories
    else if (scale_mode == 1) { s = usable[0]; for (int k = 0; k < 3; k++) { int c = usable[rng() % usable.size()]; if (c < s || rng() % 4 == 0) s = c; } }
    else { s = usable[rng() % usable.size()]; if (rng() % 3) s = usable[(size_t)(rng() % std::min<size_t>(usable.size(), 4))]; }
    if (s == 0) return 0;
    if (s == 16) return 32768;
    const int lo = 1 << (s - 1), hi = (1 << s) - 1;
    const int mag = lo + (int)(rng() % (uint32_t)(hi - lo + 1));
    return (rng() & 1u) ? mag : -mag;
}

// Encodes a predictor-1 picture of random differences; returns the stream and the picture the decoder must give back.
Bytes encode_picture(const Table & t, int width, int height, int bits, int comps, std::mt19937 & rng, int scale_mode, std::vector<uint16_t> & picture)
{
    Bytes stream = make_header(t, width, height, bits, comps);
    BitWriter bw(stream);
    const size_t row = (size_t)width * (size_t)comps;
    picture.assign(row * (size_t)height, 0);
    for (int y = 0; y < height; y++)
    {
        for (int x = 0; x < width; x++)
        {
            for (int c = 0; c < comps; c++)
            {
                int px;
                if (y == 0 && x == 0) px = 1 << (bits - 1);
                else if (y == 0) px = picture[(size_t)(x - 1) * (size_t)comps + (size_t)c];
                else if (x == 0) px = picture[(size_t)(y - 1) * row + (size_t)c];
                else px = picture[(size_t)y * row + (size_t)(x - 1) * (size_t)comps + (size_t)c];
                const int diff = draw_diff(t, rng, scale_mode);
                picture[(size_t)y * row + (size_t)x * (size_t)comps + (size_t)c] = (uint16_t)(px + diff);
                const int s = ssss_of(diff);
                size_t entry = 0;
                while (t.symbols[entry] != s) entry++;
                bw.put(t.codes[entry], t.lengths[entry]);
                if (s != 0 && s != 16) bw.put((uint32_t)(diff < 0 ? diff + (1 << s) - 1 : diff), s);
            }
        }
    }
    bw.finish();
    stream.push_back(0xFF); stream.push_back(0xD9);
    return stream;
}

size_t entropy_start(const Table & t, int comps)
{
    return 2 + 2 + (2 + 1 + 16 + t.symbols.size()) + 2 + (size_t)(8 + 3 * comps) + 2 + (size_t)(6 + 2 * comps);
}

std::vector<int> range_set(int lo, int hi) { std::vector<int> v; for (int i = lo; i <= hi; i++) v.push_back(i); return v; }

struct TableSpec { const char * name; std::vector<int> symbols; int max_len; int expect_u64; };

void run_synthetic(Counters & c, std::mt19937 & rng)
{
    const std::vector<TableSpec> specs = {
        { "fixture-like(0..14,L10)", range_set(0, 14), 10, 1 },
        { "ssss15(0..15,L16)", range_set(0, 15), 16, 1 },
        { "ssss16(0..16,L16)", range_set(0, 16), 16, 1 },
        { "ssss16(0..16,L14)", range_set(0, 16), 14, 1 },
        { "narrow(0..3,L3)", range_set(0, 3), 3, 1 },
        { "sparse(0,5,11,16;L9)", { 0, 5, 11, 16 }, 9, 1 },
        { "lone-zero(0;L1)", { 0 }, 1, 1 },
        { "ssss17(0..17,L12)", range_set(0, 17), 12, 0 },
        { "ssss20(0..20,L16)", range_set(0, 20), 16, 0 },
    };
    const int dims[][2] = { { 1, 1 }, { 1, 5 }, { 2, 2 }, { 3, 4 }, { 7, 3 }, { 16, 9 }, { 33, 17 }, { 120, 40 } };
    const int bit_depths[] = { 8, 10, 12, 14, 16 };
    for (const TableSpec & spec : specs)
    {
        for (int rep = 0; rep < 3; rep++)
        {
            const Table table = make_table(spec.symbols, spec.max_len, rng);
            for (const auto & d : dims)
            {
                const int comps = 1 + (int)(rng() % 4u);
                const int bits = bit_depths[rng() % 5u];
                const int scale_mode = (int)(rng() % 3u);
                std::vector<uint16_t> picture;
                const Bytes stream = encode_picture(table, d[0], d[1], bits, comps, rng, scale_mode, picture);
                std::string label = std::string(spec.name) + " " + std::to_string(d[0]) + "x" + std::to_string(d[1]) + "x" + std::to_string(comps) + " b" + std::to_string(bits);
                compare_readers(c, label + " encoded", stream, spec.expect_u64, &picture);

                // truncations: the last 40 bytes one by one, then a spread of cut points through the entropy data
                const size_t start = entropy_start(table, comps);
                if (stream.size() > start + 4)
                {
                    for (size_t cut = 1; cut <= 40 && cut < stream.size() - start; cut++)
                        compare_readers(c, label + " cut-" + std::to_string(cut), Bytes(stream.begin(), stream.end() - (long)cut), spec.expect_u64, nullptr, true);
                    for (int k = 1; k < 12; k++)
                    {
                        const size_t keep = start + (stream.size() - start) * (size_t)k / 12u;
                        compare_readers(c, label + " keep-" + std::to_string(keep), Bytes(stream.begin(), stream.begin() + (long)keep), spec.expect_u64, nullptr, true);
                    }
                    // the tail of a stream that ends in a stuffed 0xFF, and a stream that ends on a bare 0xFF
                    Bytes ff_end(stream.begin(), stream.end() - 2);
                    ff_end.push_back(0xFF);
                    compare_readers(c, label + " bare-FF-end", ff_end, spec.expect_u64, nullptr, true);
                    ff_end.push_back(0x00);
                    compare_readers(c, label + " FF00-end", ff_end, spec.expect_u64, nullptr, true);
                }

                // corruption: bit flips, planted 0xFF, planted 0xFF 0xD9 / 0xFF 0xD8 markers inside the entropy data
                for (int m = 0; m < 6 && stream.size() > start + 8; m++)
                {
                    Bytes bad(stream);
                    const int flips = 1 + (int)(rng() % 4u);
                    for (int f = 0; f < flips; f++)
                    {
                        const size_t at = start + (size_t)(rng() % (uint32_t)(bad.size() - start - 2));
                        const int kind = (int)(rng() % 4u);
                        if (kind == 0) bad[at] ^= (uint8_t)(1u << (rng() % 8u));
                        else if (kind == 1) bad[at] = 0xFF;
                        else if (kind == 2) { bad[at] = 0xFF; bad[at + 1] = 0xD9; }
                        else { bad[at] = 0xFF; bad[at + 1] = (uint8_t)(rng() & 0xFFu); }
                    }
                    compare_readers(c, label + " corrupt-" + std::to_string(m), bad, spec.expect_u64, nullptr, true);
                }

                // pure noise entropy at several 0xFF densities (every bit pattern, including undefined codes), plus a
                // run of all-0xFF and all-0x00 bytes
                const size_t noise_len = 1 + (size_t)(rng() % 600u);
                const int ff_percent[] = { 0, 6, 25, 60, 100 };
                for (int density : ff_percent)
                {
                    Bytes noise = make_header(table, d[0], d[1], bits, comps);
                    for (size_t i = 0; i < noise_len; i++)
                        noise.push_back((density == 100 || (int)(rng() % 100u) < density) ? 0xFF : (uint8_t)(rng() & 0xFFu));
                    compare_readers(c, label + " noise-ff" + std::to_string(density), noise, spec.expect_u64, nullptr, true);
                }
                Bytes zeros = make_header(table, d[0], d[1], bits, comps);
                zeros.insert(zeros.end(), noise_len, 0x00);
                compare_readers(c, label + " zeros", zeros, spec.expect_u64, nullptr, true);
                expect_refusal(c, label + " no-entropy", make_header(table, d[0], d[1], bits, comps));
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------------- r2 regressions
// LJ92-PRED1-DECODER-ONLY-1 r2 (sol r1 provisional verdict on 3ae57852, blockers S1, S2, S4). Each check below was run
// against 3ae57852 before its fix and failed there.

// S1: the caller may point lj92_decode's target INSIDE the compressed stream (the output overwrites bytes the decoder
// has not read yet). Master's lazy 16-bit-pair reader sees its own earlier output there; the u64 reader's 8-byte bulk
// load had already taken the original bytes, so the pixels differed. The default decode must match the legacy one.
struct AliasResult
{
    int open_ret = -1, ret = -1, u64_active = -1, measurement_active = -1;
    std::vector<uint16_t> samples;
};

AliasResult decode_aliased(const Bytes & stream, size_t target_byte, int count, bool legacy)
{
    AliasResult r;
    Bytes copy(stream.size() + 2);
    // the u16 target must be 2-aligned: slide the copy by one byte when needed
    uint8_t * base = copy.data() + ((((uintptr_t)copy.data() + target_byte) & 1u) ? 1 : 0);
    std::memcpy(base, stream.data(), stream.size());
    lj92 h = nullptr;
    int w = 0, hh = 0, bits = 0, comps = 0;
    r.open_ret = lj92_open(&h, base, (int)stream.size(), &w, &hh, &bits, &comps);
    if (r.open_ret != LJ92_ERROR_NONE) return r;
    uint16_t * target = reinterpret_cast<uint16_t *>(base + target_byte);
    lj92_set_pred1_legacy_reader(legacy ? 1 : 0);
    r.ret = lj92_decode(h, target, count, 0, nullptr, 0);
    lj92_set_pred1_legacy_reader(0);
    r.u64_active = lj92_get_last_pred1_u64_reader_active();
    r.measurement_active = lj92_get_last_pred1_fast_path_measurement_active();
    r.samples.resize((size_t)count);
    std::memcpy(r.samples.data(), target, (size_t)count * sizeof(uint16_t));
    lj92_close(h);
    return r;
}

void check_aliased_output(Counters & c, int width, int height)
{
    Table t;                                   // two codes of length 16: 0000 -> SSSS 0, 0001 -> SSSS 16
    t.symbols = { 0, 16 }; t.lengths = { 16, 16 }; t.codes = { 0u, 1u }; t.counts[16] = 2; t.max_len = 16;
    Bytes s = make_header(t, width, height, 16, 1);
    const size_t entropy = entropy_start(t, 1);
    const uint8_t body[] = { 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xD9 };
    s.insert(s.end(), body, body + sizeof(body));
    const std::string shape = std::to_string(width) + "x" + std::to_string(height);
    for (size_t offset : { (size_t)0, (size_t)2, (size_t)4 })
    {
        const std::string label = "aliased-output " + shape + " target=entropy+" + std::to_string(offset);
        const AliasResult legacy = decode_aliased(s, entropy + offset, 2, true);
        const AliasResult fresh = decode_aliased(s, entropy + offset, 2, false);
        c.checks++;
        if (legacy.open_ret != LJ92_ERROR_NONE || fresh.open_ret != LJ92_ERROR_NONE) { c.fail(label + ": open refused"); continue; }
        if (legacy.ret != fresh.ret) c.fail(label + ": decode return differs");
        if (legacy.samples != fresh.samples)
            c.fail(label + ": pixels differ from the legacy reader: legacy=[" + std::to_string(legacy.samples[0]) + "," + std::to_string(legacy.samples[1])
                   + "] default=[" + std::to_string(fresh.samples[0]) + "," + std::to_string(fresh.samples[1]) + "]");
        if (offset == 2 && legacy.samples != std::vector<uint16_t>({ 32768, 32768 }))
            c.fail(label + ": legacy (master) pixels are no longer [32768,32768]");
        if (fresh.u64_active != 0) c.fail(label + ": the u64 reader ran on an output that overlaps the compressed input");
    }
    // the same stream with the output elsewhere must still take the u64 reader: the overlap test is not a blanket fallback
    Bytes separate(s);
    lj92 h = nullptr;
    int w = 0, hh = 0, bits = 0, comps = 0;
    c.checks++;
    if (lj92_open(&h, separate.data(), (int)separate.size(), &w, &hh, &bits, &comps) != LJ92_ERROR_NONE) { c.fail("aliased-output " + shape + ": control open refused"); return; }
    uint16_t out[2] = { 0, 0 };
    const int ret = lj92_decode(h, out, 2, 0, nullptr, 0);
    if (ret != LJ92_ERROR_NONE || lj92_get_last_pred1_u64_reader_active() != 1)
        c.fail("aliased-output " + shape + ": a non-overlapping output no longer takes the u64 reader");
    lj92_close(h);
}

// S2: the oracle must not count a required-route case as checked when both readers refuse to open it.
void check_oracle_flags_unchecked_refusal(Counters & c)
{
    std::mt19937 rng(1);
    const Table lone = make_table({ 0 }, 1, rng);
    Counters probe;
    probe.quiet = true;
    compare_readers(probe, "oracle-probe", make_header(lone, 1, 1, 8, 1), 1);   // header only: both readers refuse to open
    c.checks++;
    if (probe.failures == 0) c.fail("oracle: a required-route case whose two readers both refuse to open was accepted as checked");
}

// S4: the u64-reader getter reports a decode that produced at least one sample, and a failed call resets it.
void check_getters(Counters & c)
{
    std::mt19937 rng(7);
    const Table lone = make_table({ 0 }, 1, rng);
    uint16_t buf[4] = { 0, 0, 0, 0 };
    int w = 0, hh = 0, bits = 0, comps = 0;

    Bytes frame = make_header(lone, 1, 1, 8, 1);
    frame.push_back(0x00); frame.push_back(0xFF); frame.push_back(0xD9);
    lj92 h = nullptr;
    c.checks++;
    if (lj92_open(&h, frame.data(), (int)frame.size(), &w, &hh, &bits, &comps) != LJ92_ERROR_NONE) { c.fail("getters: 1x1 control open refused"); return; }
    if (lj92_decode(h, buf, 1, 0, nullptr, 0) != LJ92_ERROR_NONE || lj92_get_last_pred1_u64_reader_active() != 1)
        c.fail("getters: a 1x1 pred-1 decode no longer reports the u64 reader (control)");
    // a refused call must not leave the previous decode's report behind
    c.checks++;
    if (lj92_decode(nullptr, buf, 1, 0, nullptr, 0) != LJ92_ERROR_BAD_HANDLE) c.fail("getters: lj92_decode(NULL) did not return BAD_HANDLE");
    if (lj92_get_last_pred1_u64_reader_active() != 0) c.fail("getters: lj92_decode(NULL) left the previous decode's u64-reader report standing");
    lj92_close(h);

    // 1x0: a pred-1 scan with no sample to decode never touches the reader
    Bytes empty = make_header(lone, 1, 0, 8, 1);
    empty.push_back(0x00); empty.push_back(0xFF); empty.push_back(0xD9);
    h = nullptr;
    c.checks++;
    if (lj92_open(&h, empty.data(), (int)empty.size(), &w, &hh, &bits, &comps) != LJ92_ERROR_NONE) { c.fail("getters: 1x0 open refused"); return; }
    const int ret = lj92_decode(h, buf, 0, 0, nullptr, 0);
    if (ret != LJ92_ERROR_NONE) c.fail("getters: 1x0 decode did not succeed");
    if (lj92_get_last_pred1_u64_reader_active() != 0) c.fail("getters: a 1x0 decode (no sample) reports the u64 reader active");
    lj92_close(h);
}

void check_bench_options(Counters & c);   // S3 checks, defined with the option parser below

int run_selftest(const std::string & root, unsigned seed)
{
    Counters c;
    std::mt19937 rng(seed);
    check_aliased_output(c, 2, 1);
    check_aliased_output(c, 1, 2);
    check_oracle_flags_unchecked_refusal(c);
    check_getters(c);
    check_bench_options(c);
    int fixture_frames = 0;
    for (const char * fixture : kFixtures)
    {
        Bytes file;
        if (root.empty() || !read_file(root + "/" + fixture, file)) { std::fprintf(stderr, "NOTE: fixture %s not readable under root '%s'\n", fixture, root.c_str()); c.fail(std::string("fixture unreadable: ") + fixture); continue; }
        std::vector<Bytes> frames;
        collect_lj92_payloads(file, frames);
        if (frames.empty()) { c.fail(std::string("fixture holds no lossless frame: ") + fixture); continue; }
        for (size_t i = 0; i < frames.size(); i++)
        {
            const DecodeInfo probe = decode_copy(frames[i], false);
            const int want_u64 = (probe.predictor == 1 && probe.eligible && probe.max_ssss <= 16) ? 1 : 0;
            compare_readers(c, std::string(fixture) + " frame " + std::to_string(i), frames[i], want_u64);
            if (i == 0)
                std::printf("fixture %s: %zu lossless frame(s); frame 0 %dx%d bits=%d components=%d predictor=%d scan_components=%d max_ssss=%d eligible=%d u64_taken=%d\n",
                            fixture, frames.size(), probe.width, probe.height, probe.bits, probe.components, probe.predictor,
                            probe.scan_components, probe.max_ssss, probe.eligible, probe.u64_active);
            // the fixtures must exercise the reader this card changes: pin that
            if (probe.predictor != 1 || !probe.eligible) c.fail(std::string(fixture) + " frame " + std::to_string(i) + " is not on the pred-1 fast path");
            if (want_u64 != probe.u64_active) c.fail(std::string(fixture) + " frame " + std::to_string(i) + " u64 reader not taken on an eligible layout");
            fixture_frames++;
            // every fixture frame, truncated at every byte of its last 40 and at a spread of points
            const Bytes & f = frames[i];
            if (i % 8 == 0)
            {
                for (size_t cut = 1; cut <= 40 && cut < f.size(); cut++)
                    compare_readers(c, std::string(fixture) + " frame " + std::to_string(i) + " cut-" + std::to_string(cut), Bytes(f.begin(), f.end() - (long)cut), -1, nullptr, true);
                for (int k = 1; k < 8; k++)
                    compare_readers(c, std::string(fixture) + " frame " + std::to_string(i) + " keep-" + std::to_string(k), Bytes(f.begin(), f.begin() + (long)(f.size() * (size_t)k / 8u)), -1, nullptr, true);
            }
        }
    }
    run_synthetic(c, rng);
    std::printf("selftest seed=%u fixture_frames=%d checks=%d refusals=%d failures=%d\n", seed, fixture_frames, c.checks, c.refusals, c.failures);
    std::printf(c.failures == 0 ? "SELFTEST PASS\n" : "SELFTEST FAIL\n");
    return c.failures == 0 ? 0 : 1;
}

// ---------------------------------------------------------------------------------------------------- bench

double now_ms()
{
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

double percentile(std::vector<double> v, double p)
{
    if (v.empty()) return 0.0;
    std::sort(v.begin(), v.end());
    const double pos = p * (double)(v.size() - 1);
    const size_t lo = (size_t)pos;
    const size_t hi = std::min(lo + 1, v.size() - 1);
    return v[lo] + (v[hi] - v[lo]) * (pos - (double)lo);
}

bool load_frames(const std::string & root, std::vector<Bytes> & frames, std::vector<std::string> & origin)
{
    for (const char * fixture : kFixtures)
    {
        Bytes file;
        if (!read_file(root + "/" + fixture, file)) continue;
        std::vector<Bytes> got;
        collect_lj92_payloads(file, got);
        for (auto & g : got) { frames.push_back(std::move(g)); origin.push_back(fixture); }
    }
    return !frames.empty();
}

struct Opened
{
    Bytes stream;
    lj92 handle = nullptr;
    int width = 0, height = 0, bits = 0, components = 0;
    std::vector<uint16_t> out;
};

bool open_frame(Opened & o, const Bytes & payload)
{
    o.stream = payload;
    if (lj92_open(&o.handle, o.stream.data(), (int)o.stream.size(), &o.width, &o.height, &o.bits, &o.components) != LJ92_ERROR_NONE) return false;
    o.out.assign((size_t)o.width * (size_t)o.height * (size_t)o.components, 0);
    return true;
}

// r2d (sol r1 S3): the route one decode actually took, read from the decoder's own getters on the calling thread right
// after the call. A timing is attributed to the arm it is labelled with only when this says so.
struct Route
{
    int fast_active = 0, u64_active = 0, measurement_active = 0;
};

// Empty when the arm ran the route its label claims; otherwise why it did not. A generic-route decode (for example under
// MLVAPP_PROFILE_LJ92_GENERIC_SPLIT) is no timing of either reader, and the new arm must have run the u64 reader.
std::string route_problem(const Route & r, bool legacy_arm)
{
    if (!r.fast_active) return "the predictor-1 fast path did not run, so this is not a reader timing (generic route)";
    if (legacy_arm && r.u64_active) return "the forced-legacy arm ran the u64 reader";
    if (!legacy_arm && !r.u64_active) return "the new arm did not run the u64 reader";
    return std::string();
}

// bitstream_ms / predictor_ms: the decoder's own split, filled only when the measurement route is active
// (MLVAPP_PRED1_FASTPATH_MEASUREMENT; that route decodes a row of differences, then runs the predictor, so its total is not
// the production loop's). Returns the elapsed ms, or a negative number when the decode failed: a failed decode is never a
// sample, the callers refuse the run.
double time_decode(Opened & o, bool legacy, Route & route, double * bitstream_ms = nullptr, double * predictor_ms = nullptr)
{
    lj92_set_pred1_legacy_reader(legacy ? 1 : 0);
    const double t0 = now_ms();
    const int ret = lj92_decode(o.handle, o.out.data(), (int)o.out.size(), 0, nullptr, 0);
    const double t1 = now_ms();
    lj92_set_pred1_legacy_reader(0);
    route.fast_active = lj92_get_last_pred1_fast_path_active();
    route.u64_active = lj92_get_last_pred1_u64_reader_active();
    route.measurement_active = lj92_get_last_pred1_fast_path_measurement_active();
    if (bitstream_ms) *bitstream_ms = lj92_get_last_pred1_fast_path_bitstream_ms();
    if (predictor_ms) *predictor_ms = lj92_get_last_pred1_fast_path_predictor_ms();
    return ret == LJ92_ERROR_NONE ? t1 - t0 : -1.0;
}

int run_bench(const std::string & root, int iters)
{
    std::vector<Bytes> frames;
    std::vector<std::string> origin;
    if (!load_frames(root, frames, origin)) { std::fprintf(stderr, "no fixture frames\n"); return 2; }
    // a bounded, evenly spread sample of the large fixture keeps a run short; the tiny one is taken whole
    std::vector<size_t> pick;
    for (size_t i = 0; i < frames.size(); i++) pick.push_back(i);
    if (pick.size() > 48) { std::vector<size_t> s; for (size_t k = 0; k < 48; k++) s.push_back(pick[k * pick.size() / 48]); pick = s; }
    std::vector<Opened> opened(pick.size());
    auto close_all = [&]() { for (auto & o : opened) if (o.handle) { lj92_close(o.handle); o.handle = nullptr; } };
    for (size_t k = 0; k < pick.size(); k++) if (!open_frame(opened[k], frames[pick[k]])) { std::fprintf(stderr, "open failed frame %zu\n", pick[k]); close_all(); return 2; }
    std::vector<std::vector<double>> legacy_t(pick.size()), fresh_t(pick.size());
    // r2d (sol r1 S3): every decode, warm-up included, must have run the route its arm is labelled with; the split columns come
    // from the decoder's own measurement-active report, not from the environment.
    bool split = false;
    auto arm_ok = [&](const char * phase, size_t k, bool legacy_arm, double ms, const Route & r) -> bool {
        std::string why;
        if (ms < 0) why = "the decode failed";
        else
        {
            why = route_problem(r, legacy_arm);
            if (why.empty() && (r.measurement_active != 0) != split) why = "the measurement route is not the same for every decode";
        }
        if (why.empty()) return true;
        std::fprintf(stderr, "bench refused (%s, frame %zu, %s arm): %s\n", phase, pick[k], legacy_arm ? "legacy" : "new", why.c_str());
        return false;
    };
    // warm-up pass (not recorded); it also proves the route before anything is timed
    for (size_t k = 0; k < opened.size(); k++)
    {
        Route rl, rf;
        const double wl = time_decode(opened[k], true, rl);
        const double wf = time_decode(opened[k], false, rf);
        if (k == 0) split = rf.measurement_active != 0;
        if (!arm_ok("warm-up", k, true, wl, rl) || !arm_ok("warm-up", k, false, wf, rf)) { close_all(); return 3; }
    }
    std::vector<double> bs_l, pr_l, bs_f, pr_f;
    for (int it = 0; it < iters; it++)
    {
        for (size_t k = 0; k < opened.size(); k++)
        {
            const bool legacy_first = ((it + (int)k) & 1) == 0;
            double bs_a = 0, pr_a = 0, bs_b = 0, pr_b = 0;
            Route ra, rb;
            double a = time_decode(opened[k], legacy_first, ra, &bs_a, &pr_a);
            double b = time_decode(opened[k], !legacy_first, rb, &bs_b, &pr_b);
            if (!arm_ok("timed", k, legacy_first, a, ra) || !arm_ok("timed", k, !legacy_first, b, rb)) { close_all(); return 3; }
            (legacy_first ? legacy_t : fresh_t)[k].push_back(a);
            (legacy_first ? fresh_t : legacy_t)[k].push_back(b);
            if (split)
            {
                (legacy_first ? bs_l : bs_f).push_back(bs_a); (legacy_first ? pr_l : pr_f).push_back(pr_a);
                (legacy_first ? bs_f : bs_l).push_back(bs_b); (legacy_first ? pr_f : pr_l).push_back(pr_b);
            }
        }
    }
    if (split)
        std::printf("SPLIT median ms/frame (measurement route): legacy bitstream=%.3f predictor=%.3f | new bitstream=%.3f predictor=%.3f\n",
                    percentile(bs_l, 0.5), percentile(pr_l, 0.5), percentile(bs_f, 0.5), percentile(pr_f, 0.5));
    std::vector<double> all_l, all_f, med_l, med_f;
    for (size_t k = 0; k < pick.size(); k++)
    {
        all_l.insert(all_l.end(), legacy_t[k].begin(), legacy_t[k].end());
        all_f.insert(all_f.end(), fresh_t[k].begin(), fresh_t[k].end());
        med_l.push_back(percentile(legacy_t[k], 0.5));
        med_f.push_back(percentile(fresh_t[k], 0.5));
    }
    const Opened & first = opened[0];
    std::printf("BENCH frames=%zu iters=%d picture=%dx%dx%d (%.2f MP samples/frame)\n", pick.size(), iters, first.width, first.height, first.components,
                (double)first.out.size() / 1e6);
    std::printf("legacy  min=%.4f median=%.4f p95=%.4f ms/frame (median of per-frame medians %.4f)\n", percentile(all_l, 0.0), percentile(all_l, 0.5), percentile(all_l, 0.95), percentile(med_l, 0.5));
    std::printf("new     min=%.4f median=%.4f p95=%.4f ms/frame (median of per-frame medians %.4f)\n", percentile(all_f, 0.0), percentile(all_f, 0.5), percentile(all_f, 0.95), percentile(med_f, 0.5));
    const double ml = percentile(all_l, 0.5), mf = percentile(all_f, 0.5);
    std::printf("BENCH_RESULT median_legacy_ms=%.4f median_new_ms=%.4f change_pct=%.1f\n", ml, mf, ml > 0 ? (mf - ml) / ml * 100.0 : 0.0);
    close_all();
    return 0;
}

// ---------------------------------------------------------------------------------------------------- worker scaling

int run_scale(const std::string & root, double seconds)
{
    std::vector<Bytes> frames;
    std::vector<std::string> origin;
    if (!load_frames(root, frames, origin)) { std::fprintf(stderr, "no fixture frames\n"); return 2; }
    std::vector<size_t> pick;
    const size_t want = std::min<size_t>(frames.size(), 24);
    for (size_t k = 0; k < want; k++) pick.push_back(k * frames.size() / want);
    const int worker_counts[] = { 1, 2, 4, 8 };
    std::printf("SCALE hardware_threads=%u frames_in_ring=%zu seconds_per_run=%.1f\n", std::thread::hardware_concurrency(), pick.size(), seconds);
    for (int workers : worker_counts)
    {
        for (int mode = 0; mode < 2; mode++)
        {
            const bool legacy = mode == 0;
            std::atomic<bool> go(false);
            std::atomic<int> refused(0);   // r2d (sol r1 S3): set by the first worker whose decode failed or ran the wrong route
            std::vector<std::vector<double>> lat((size_t)workers);
            std::vector<double> samples_done((size_t)workers, 0.0);
            std::vector<double> wall((size_t)workers, 0.0);
            std::vector<std::thread> threads;
            for (int w = 0; w < workers; w++)
            {
                threads.emplace_back([&, w]() {
                    // RAM-resident payload copies and a private decoder and output buffer per worker; no file I/O inside the timed loop
                    std::vector<Opened> own(pick.size());
                    bool opened_all = true;
                    for (size_t k = 0; k < pick.size(); k++) if (!open_frame(own[k], frames[pick[k]])) opened_all = false;
                    if (!opened_all) { int none = 0; if (refused.compare_exchange_strong(none, 1)) std::fprintf(stderr, "scale refused (worker %d): a fixture frame did not open\n", w); }
                    while (!go.load()) std::this_thread::yield();
                    const double start = now_ms();
                    size_t k = (size_t)w;
                    double done = 0.0;
                    while (refused.load() == 0 && now_ms() - start < seconds * 1000.0)
                    {
                        Opened & o = own[k % own.size()];
                        Route r;
                        const double ms = time_decode(o, legacy, r);
                        std::string why;
                        if (ms < 0) why = "the decode failed";
                        else if (!(why = route_problem(r, legacy)).empty()) {}
                        else if (r.measurement_active) why = "the measurement route is active, so the total is not the production loop's";
                        if (!why.empty())
                        {
                            int none = 0;
                            if (refused.compare_exchange_strong(none, 2))
                                std::fprintf(stderr, "scale refused (worker %d, %s reader): %s\n", w, legacy ? "legacy" : "new", why.c_str());
                            break;
                        }
                        lat[(size_t)w].push_back(ms); done += (double)o.out.size();
                        k++;
                    }
                    wall[(size_t)w] = now_ms() - start;
                    samples_done[(size_t)w] = done;
                    for (auto & o : own) if (o.handle) lj92_close(o.handle);
                });
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(300));
            go.store(true);
            for (auto & t : threads) t.join();
            if (refused.load() != 0) return 3;   // no SCALE_RESULT line for a run that did not measure its labelled reader
            std::vector<double> all;
            double total = 0.0, longest = 0.0;
            for (int w = 0; w < workers; w++) { all.insert(all.end(), lat[(size_t)w].begin(), lat[(size_t)w].end()); total += samples_done[(size_t)w]; longest = std::max(longest, wall[(size_t)w]); }
            std::printf("SCALE_RESULT workers=%d reader=%s frames=%zu aggregate_MPs=%.1f latency_p50_ms=%.4f latency_p95_ms=%.4f\n", workers, legacy ? "legacy" : "new",
                        all.size(), total / 1e6 / (longest / 1000.0), percentile(all, 0.5), percentile(all, 0.95));
        }
    }
    return 0;
}

// ---------------------------------------------------------------------------------------------------- options

struct Args
{
    std::string mode, root;
    unsigned seed = 20261120u;
    int iters = 15;
    double seconds = 4.0;
};

// r2d (sol r1 S3): strict option values. Digits only for the integers (no sign, no space, no suffix), a plain decimal for
// --seconds; a value outside the bounds, a missing value and an unknown option are all usage errors, never a silent default.
bool parse_uint_arg(const char * s, unsigned long long lo, unsigned long long hi, unsigned long long & out)
{
    if (s == nullptr || *s == 0 || std::strlen(s) > 19) return false;
    unsigned long long v = 0;
    for (const char * p = s; *p; p++)
    {
        if (*p < '0' || *p > '9') return false;
        v = v * 10ull + (unsigned long long)(*p - '0');
    }
    if (v < lo || v > hi) return false;
    out = v;
    return true;
}

bool parse_seconds_arg(const char * s, double & out)
{
    if (s == nullptr || *s == 0) return false;
    for (const char * p = s; *p; p++) if (!std::strchr("0123456789.eE+-", *p)) return false;
    char * end = nullptr;
    const double v = std::strtod(s, &end);
    if (end == s || *end != 0 || !std::isfinite(v) || v <= 0.0 || v > 3600.0) return false;
    out = v;
    return true;
}

// Returns 0 and fills args, or 2 with the reason in err.
int parse_args(const std::vector<std::string> & argv, Args & args, std::string & err)
{
    for (size_t i = 0; i < argv.size(); i++)
    {
        const std::string & a = argv[i];
        if (a == "--selftest" || a == "--bench" || a == "--scale")
        {
            if (!args.mode.empty() && args.mode != a) { err = "two modes given: " + args.mode + " and " + a; return 2; }
            args.mode = a;
            continue;
        }
        if (a != "--seed" && a != "--root" && a != "--iters" && a != "--seconds") { err = "unknown option: " + a; return 2; }
        if (i + 1 >= argv.size()) { err = "option " + a + " needs a value"; return 2; }
        const char * v = argv[++i].c_str();
        unsigned long long n = 0;
        if (a == "--root") args.root = v;
        else if (a == "--seed")
        {
            if (!parse_uint_arg(v, 0ull, 0xFFFFFFFFull, n)) { err = "--seed wants an integer 0..4294967295, got '" + std::string(v) + "'"; return 2; }
            args.seed = (unsigned)n;
        }
        else if (a == "--iters")
        {
            if (!parse_uint_arg(v, 1ull, 100000ull, n)) { err = "--iters wants an integer 1..100000, got '" + std::string(v) + "'"; return 2; }
            args.iters = (int)n;
        }
        else if (!parse_seconds_arg(v, args.seconds)) { err = "--seconds wants a finite number 0 < S <= 3600, got '" + std::string(v) + "'"; return 2; }
    }
    return 0;
}

void check_bench_options(Counters & c)
{
    auto parses = [](std::vector<std::string> v) { Args a; std::string e; return parse_args(v, a, e) == 0; };
    const struct { const char * label; std::vector<std::string> argv; bool ok; } cases[] = {
        { "plain bench", { "--bench" }, true },
        { "iters 1", { "--bench", "--iters", "1" }, true },
        { "iters 100000", { "--bench", "--iters", "100000" }, true },
        { "iters 0", { "--bench", "--iters", "0" }, false },
        { "iters 100001", { "--bench", "--iters", "100001" }, false },
        { "iters garbage", { "--bench", "--iters", "abc" }, false },
        { "iters signed", { "--bench", "--iters", "-5" }, false },
        { "iters suffix", { "--bench", "--iters", "5x" }, false },
        { "iters empty", { "--bench", "--iters", "" }, false },
        { "seed garbage", { "--bench", "--seed", "x" }, false },
        { "seed too big", { "--selftest", "--seed", "4294967296" }, false },
        { "seconds 1", { "--scale", "--seconds", "1" }, true },
        { "seconds 0.5", { "--scale", "--seconds", "0.5" }, true },
        { "seconds 3600", { "--scale", "--seconds", "3600" }, true },
        { "seconds 1e308", { "--scale", "--seconds", "1e308" }, false },
        { "seconds 3601", { "--scale", "--seconds", "3601" }, false },
        { "seconds nan", { "--scale", "--seconds", "nan" }, false },
        { "seconds inf", { "--scale", "--seconds", "inf" }, false },
        { "seconds zero", { "--scale", "--seconds", "0" }, false },
        { "seconds negative", { "--scale", "--seconds", "-3" }, false },
        { "seconds hex", { "--scale", "--seconds", "0x10" }, false },
        { "seconds underflow", { "--scale", "--seconds", "1e-400" }, false },
        { "unknown option", { "--bench", "--bogus" }, false },
        { "missing value", { "--bench", "--iters" }, false },
        { "missing root value", { "--bench", "--root" }, false },
        { "two modes", { "--bench", "--scale" }, false },
    };
    for (const auto & t : cases)
    {
        c.checks++;
        if (parses(t.argv) != t.ok) c.fail(std::string("options: '") + t.label + "' was " + (t.ok ? "refused" : "accepted"));
    }
    // a timing is attributed to the reader that really ran
    Route generic, legacy_arm, new_arm, new_on_legacy;
    legacy_arm.fast_active = 1;
    new_arm.fast_active = 1; new_arm.u64_active = 1;
    new_on_legacy.fast_active = 1;
    const struct { const char * label; Route r; bool legacy; bool ok; } routes[] = {
        { "generic route, legacy arm", generic, true, false },
        { "generic route, new arm", generic, false, false },
        { "legacy arm on the legacy reader", legacy_arm, true, true },
        { "new arm on the u64 reader", new_arm, false, true },
        { "new arm that fell back to the legacy reader", new_on_legacy, false, false },
        { "legacy arm that ran the u64 reader", new_arm, true, false },
    };
    for (const auto & t : routes)
    {
        c.checks++;
        if (route_problem(t.r, t.legacy).empty() != t.ok) c.fail(std::string("route: '") + t.label + "' was " + (t.ok ? "refused" : "accepted"));
    }
}

} // namespace

// tests/pipeline/test_lj92_pred1_reader.cpp includes this file to run run_selftest() inside pipeline_tests (so CI runs it);
// it defines the macro below to leave this main out.
#ifndef LJ92_PRED1_READER_TOOL_NO_MAIN
int main(int argc, char ** argv)
{
    const char * const usage = "usage: lj92_pred1_reader_tool --selftest|--bench|--scale [--seed N] [--iters 1..100000] [--seconds 0<S<=3600] [--root DIR]\n";
    Args args;
    std::string err;
    if (parse_args(std::vector<std::string>(argv + 1, argv + argc), args, err) != 0)
    {
        std::fprintf(stderr, "lj92_pred1_reader_tool: %s\n%s", err.c_str(), usage);
        return 2;
    }
    const std::string root = find_root(args.root);
    if (args.mode == "--selftest") return run_selftest(root, args.seed);
    if (args.mode == "--bench") return run_bench(root, args.iters);
    if (args.mode == "--scale") return run_scale(root, args.seconds);
    std::fprintf(stderr, "%s", usage);
    return 2;
}
#endif // LJ92_PRED1_READER_TOOL_NO_MAIN
