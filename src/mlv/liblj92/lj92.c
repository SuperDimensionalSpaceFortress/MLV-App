/*
lj92.c
(c) Andrew Baldwin 2014

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/

#include <stdint.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../debug/StageTiming.h"
#include "lj92.h"

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;

#if defined(__GNUC__)
#define LJ92_ALWAYS_INLINE inline __attribute__((always_inline))
#elif defined(_MSC_VER)
#define LJ92_ALWAYS_INLINE __forceinline
#else
#define LJ92_ALWAYS_INLINE inline
#endif

static MLV_STAGE_THREAD_LOCAL int g_lj92_last_pred6_split_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_pred6_split_requested = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_generic_split_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_generic_split_requested = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_pred1_fast_path_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_pred1_fast_path_measurement_requested = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_pred1_fast_path_measurement_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_pred1_fast_path_eligible = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_scan_component_count = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_write_length = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_expected_write_length = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_skip_length = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_linearize_active = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_component_count = 0;
static MLV_STAGE_THREAD_LOCAL int g_lj92_last_predictor = -1;
static MLV_STAGE_THREAD_LOCAL double g_lj92_last_pred6_total_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_lj92_last_pred6_bitstream_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_lj92_last_pred6_predictor_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_lj92_last_generic_total_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_lj92_last_generic_bitstream_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_lj92_last_generic_predictor_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_lj92_last_pred1_fast_path_total_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_lj92_last_pred1_fast_path_bitstream_ms = 0.0;
static MLV_STAGE_THREAD_LOCAL double g_lj92_last_pred1_fast_path_predictor_ms = 0.0;

//#define SLOW_HUFF
//#define DEBUG

typedef struct _ljp {
    u8* data;
    u8* dataend;
    int datalen;
    int scanstart;
    int ix;
    int x; // Width
    int y; // Height
    int bits; // Bit depth
    int components;  // Components(Nf)
    int writelen; // Write rows this long
    int skiplen; // Skip this many values after each row
    u16* linearize; // Linearization table
    int linlen;
    //int sssshist[16];

    // Huffman table - only one supported, and probably needed
#ifdef SLOW_HUFF
    int* maxcode;
    int* mincode;
    int* valptr;
    u8* huffval;
    int* huffsize;
    int* huffcode;
#else
    u32* hufflut;
    int huffbits;
#endif
    // Parse state
    int cnt;
    u32 b;
    u16* image;
    u16* rowcache;
    u16* outrow[2];
} ljp;

static int find(ljp* self) {
    int ix = self->ix;
    u8* data = self->data;
    if (ix < 0 || ix >= self->datalen) return -1; /* r6 A2 class: never read data[ix] past the payload */
    while (data[ix] != 0xFF && ix<(self->datalen-1)) {
        ix += 1;
    }
    ix += 2;
    if (ix>=self->datalen) return -1;
    self->ix = ix;
    return data[ix-1];
}

#define BEH(ptr) ((((int)(*&ptr))<<8)|(*(&ptr+1)))

/* PLAYBACK-SOURCE-FRAME-IDENTITY-1 r6 (adversary r5 pass 3 A2): every marker segment length is read through
 * this, and a segment whose length field or whose end (ix + length) is not inside the payload is refused
 * (LJ92_ERROR_CORRUPT) by its caller. Returns the BEH length at ix, or -1 when ix or ix + 1 is past datalen. */
static int lj92_segment_length(const ljp* self, int ix) {
    if (ix < 0 || ix >= self->datalen - 1) return -1;
    return BEH(self->data[ix]);
}

/* r6 A2: ix + length stays inside the payload (strictly before datalen: something must follow every
 * segment, at least the next marker or the entropy data). Overflow-safe for any datalen <= INT_MAX. */
static int lj92_segment_fits(const ljp* self, int ix, int length) {
    return length >= 0 && ix >= 0 && ix < self->datalen && length < self->datalen - ix;
}

/* r6 A2: where the entropy data of the scan at scanstart begins, or -1 when the SOS header runs past the
 * payload. Every scan variant starts here instead of an unbounded ix += BEH(data[scanstart]). */
static int lj92_scan_entropy_start(const ljp* self) {
    const int length = lj92_segment_length(self, self->scanstart);
    if (!lj92_segment_fits(self, self->scanstart, length)) return -1;
    return self->scanstart + length;
}

/* r6 (adversary r5 pass 3 A1/A2): the bit reader's two-byte load. It never reads past datalen: a scan whose
 * entropy data ends before its last sample reads zero bytes from there on, so one payload decodes to one
 * picture whatever lies after it in memory (the caller's slack, or nothing at all). The load is the same
 * little-endian u16 the reader always used while both bytes are inside the payload. */
/* r9 (adversary r9 pass 1 A1 on 76d8340b): overflow-free -- `ix + 1` overflowed when ix reached INT_MAX. */
static LJ92_ALWAYS_INLINE int lj92_load_entropy_pair(const u8* data, int ix, int datalen) {
    if (ix >= 0 && ix < datalen - 1) return *(const u16*)&data[ix];
    return (ix >= 0 && ix < datalen) ? data[ix] : 0;
}

/* r9 (adversary r9 pass 1 A1 on 76d8340b): the entropy index never passes datalen. Past the payload every load is zero, so
 * an index at datalen reads exactly what one further on would; it now stops there. A picture with more samples than its
 * payload can feed (65535 x 10000 at 31 bits a sample) used to keep advancing it past INT_MAX -- signed overflow, and a
 * wrapped index that admitted a load outside the payload. Whether ix >= datalen is decided as before: the step that took
 * it to or past datalen now takes it to datalen. Assumes 0 <= ix <= datalen (every scan starts inside the payload). */
static LJ92_ALWAYS_INLINE int lj92_advance_entropy_ix(int ix, int step, int datalen) {
    return datalen - ix > step ? ix + step : datalen;
}

#ifndef SLOW_HUFF
#define LJ92_HUFF_DIRECT_FLAG 0x80000000u
#define LJ92_HUFF_DIRECT_BITS_MASK 0x1Fu
#define LJ92_HUFF_DIRECT_DIFF_SHIFT 5
#define LJ92_HUFF_DIRECT_DIFF_BIAS 32767

// Predecoded entries let nextdiff_fast() skip payload refill/sign-extend when
// the initial huffbits-wide peek already contains the whole symbol.
static LJ92_ALWAYS_INLINE u32 lj92_pack_huff_fallback(int ssss, int usedbits)
{
    return ((u32)ssss << 8) | (u32)usedbits;
}

static LJ92_ALWAYS_INLINE u32 lj92_pack_huff_direct(int diff, int total_used_bits)
{
    return LJ92_HUFF_DIRECT_FLAG
        | ((u32)(diff + LJ92_HUFF_DIRECT_DIFF_BIAS) << LJ92_HUFF_DIRECT_DIFF_SHIFT)
        | (u32)total_used_bits;
}

static LJ92_ALWAYS_INLINE int lj92_unpack_huff_direct_diff(u32 entry)
{
    return (int)((entry >> LJ92_HUFF_DIRECT_DIFF_SHIFT) & 0xFFFFu) - LJ92_HUFF_DIRECT_DIFF_BIAS;
}
#endif

static int parseHuff(ljp* self) {
    int ret = LJ92_ERROR_CORRUPT;
    /* r6 A2 class: the 19-byte DHT head (length, class/id, 16 counts) is read and written in place, so it
     * must lie inside the payload before the class/id byte is zeroed or a count is read. */
    if (self->ix < 0 || self->ix > self->datalen - 19) return ret;
    u8* huffhead = &self->data[self->ix]; // xstruct.unpack('>HB16B',self.data[self.ix:self.ix+19])
    u8* bits = &huffhead[2];
    bits[0] = 0; // Because table starts from 1
    int hufflen = BEH(huffhead[0]);
    if (!lj92_segment_fits(self, self->ix, hufflen)) return ret;
    /* r6 A2 class: the symbol values the table fill reads (sum of the 16 counts, after the head) are inside
     * the payload too. */
    int symbols = 0;
    for (int level = 1; level <= 16; level++) symbols += bits[level];
    if (symbols > self->datalen - self->ix - 19) return ret;
#ifdef SLOW_HUFF
    u8* huffval = calloc(hufflen - 19,sizeof(u8));
    if (huffval == NULL) return LJ92_ERROR_NO_MEMORY;
    self->huffval = huffval;
    for (int hix=0;hix<(hufflen-19);hix++) {
        huffval[hix] = self->data[self->ix+19+hix];
#ifdef DEBUG
        printf("huffval[%d]=%d\n",hix,huffval[hix]);
#endif
    }
    self->ix += hufflen;
    // Generate huffman table
    int k = 0;
    int i = 1;
    int j = 1;
    int huffsize_needed = 1;
    // First calculate how long huffsize needs to be
    while (i<=16) {
        while (j<=bits[i]) {
            huffsize_needed++;
            k = k+1;
            j = j+1;
        }
        i = i+1;
        j = 1;
    }
    // Now allocate and do it
    int* huffsize = calloc(huffsize_needed,sizeof(int));
    if (huffsize == NULL) return LJ92_ERROR_NO_MEMORY;
    self->huffsize = huffsize;
    k = 0;
    i = 1;
    j = 1;
    // First calculate how long huffsize needs to be
    int hsix = 0;
    while (i<=16) {
        while (j<=bits[i]) {
            huffsize[hsix++] = i;
            k = k+1;
            j = j+1;
        }
        i = i+1;
        j = 1;
    }
    huffsize[hsix++] = 0;

    // Calculate the size of huffcode array
    int huffcode_needed = 0;
    k = 0;
    int code = 0;
    int si = huffsize[0];
    while (1) {
        while (huffsize[k] == si) {
            huffcode_needed++;
            code = code+1;
            k = k+1;
        }
        if (huffsize[k] == 0)
            break;
        while (huffsize[k] != si) {
            code = code << 1;
            si = si + 1;
        }
    }
    // Now fill it
    int* huffcode = calloc(huffcode_needed,sizeof(int));
    if (huffcode == NULL) return LJ92_ERROR_NO_MEMORY;
    self->huffcode = huffcode;
    int hcix = 0;
    k = 0;
    code = 0;
    si = huffsize[0];
    while (1) {
        while (huffsize[k] == si) {
            huffcode[hcix++] = code;
            code = code+1;
            k = k+1;
        }
        if (huffsize[k] == 0)
            break;
        while (huffsize[k] != si) {
            code = code << 1;
            si = si + 1;
        }
    }

    i = 0;
    j = 0;

    int* maxcode = calloc(17,sizeof(int));
    if (maxcode == NULL) return LJ92_ERROR_NO_MEMORY;
    self->maxcode = maxcode;
    int* mincode = calloc(17,sizeof(int));
    if (mincode == NULL) return LJ92_ERROR_NO_MEMORY;
    self->mincode = mincode;
    int* valptr = calloc(17,sizeof(int));
    if (valptr == NULL) return LJ92_ERROR_NO_MEMORY;
    self->valptr = valptr;

    while (1) {
        while (1) {
            i++;
            if (i>16)
                break;
            if (bits[i]!=0)
                break;
            maxcode[i] = -1;
        }
        if (i>16)
            break;
        valptr[i] = j;
        mincode[i] = huffcode[j];
        j = j+bits[i]-1;
        maxcode[i] = huffcode[j];
        j++;
    }
    free(huffsize);
    self->huffsize = NULL;
    free(huffcode);
    self->huffcode = NULL;
    ret = LJ92_ERROR_NONE;
#else
    /* Calculate huffman direct lut */
    // How many bits in the table - find highest entry
    u8* huffvals = &self->data[self->ix+19];
    int maxbits = 16;
    while (maxbits>0) {
        if (bits[maxbits]) break;
        maxbits--;
    }
    /* r6 (A1 class): a table with no codes leaves the lookup table unwritten, and every sample would be read
     * from uninitialised memory; it can decode nothing, so it is refused. */
    if (maxbits == 0) return LJ92_ERROR_CORRUPT;
    self->huffbits = maxbits;
    /* Now fill the lut */
    u32* hufflut = malloc((1<<maxbits) * sizeof(u32));
    if (hufflut == NULL) return LJ92_ERROR_NO_MEMORY;
    self->hufflut = hufflut;
    int i = 0;
    int hv = 0;
    int rv = 0;
    int vl = 0; // i
    int hcode;
    int bitsused = 1;
#ifdef DEBUG
    printf("%04x:%x:%d:%x\n",i,huffvals[hv],bitsused,1<<(maxbits-bitsused));
#endif
    while (i<1<<maxbits) {
        if (bitsused>maxbits) {
            break; // Done. Should never get here!
        }
        if (vl >= bits[bitsused]) {
            bitsused++;
            vl = 0;
            continue;
        }
        if (rv == 1 << (maxbits-bitsused)) {
            rv = 0;
            vl++;
            hv++;
#ifdef DEBUG
            printf("%04x:%x:%d:%x\n",i,huffvals[hv],bitsused,1<<(maxbits-bitsused));
#endif
            continue;
        }
        hcode = huffvals[hv];
        if (hcode == 0) {
            hufflut[i] = lj92_pack_huff_direct(0, bitsused);
        } else if (hcode == 16) {
            hufflut[i] = lj92_pack_huff_direct(1 << 15, bitsused);
        } else if (bitsused + hcode <= maxbits) {
            const int payload_bits_available = maxbits - bitsused;
            const int suffix_mask = (1 << payload_bits_available) - 1;
            const int payload = (i & suffix_mask) >> (payload_bits_available - hcode);
            int diff = payload;
            const int vt = 1 << (hcode - 1);
            if (diff < vt) {
                diff += 1 - (1 << hcode);
            }
            hufflut[i] = lj92_pack_huff_direct(diff, bitsused + hcode);
        } else {
            hufflut[i] = lj92_pack_huff_fallback(hcode, bitsused);
        }
        //printf("%d %d %d\n",i,bitsused,hcode);
        i++;
        rv++;
    }
    /* r6 (A1 class): codes the table does not define (every JPEG table leaves at least the all-ones code
     * free) used to stay uninitialised, so a bitstream that spells one decoded whatever the heap held, a
     * different picture per call. They now decode as a zero difference over maxbits bits, the same every call. */
    while (i < 1<<maxbits) {
        hufflut[i++] = lj92_pack_huff_direct(0, maxbits);
    }
    ret = LJ92_ERROR_NONE;
#endif
    return ret;
}

static int parseSof3(ljp* self) {
    /* r6 A2 class: SOF3 reads bytes ix..ix+7 (the component count is ix+7, which the old ix+6 bound missed),
     * and its length must end inside the payload. */
    if (self->ix < 0 || self->ix+7 >= self->datalen) return LJ92_ERROR_CORRUPT;
    const int length = lj92_segment_length(self, self->ix);
    if (!lj92_segment_fits(self, self->ix, length)) return LJ92_ERROR_CORRUPT;
    self->y = BEH(self->data[self->ix+3]);
    self->x = BEH(self->data[self->ix+5]);
    self->bits = self->data[self->ix+2];
    self->components = self->data[self->ix + 7];
    self->ix += length;
    return LJ92_ERROR_NONE;
}

static int parseBlock(ljp* self) {
    /* r6 A2 class: the length field itself and the segment end are both inside the payload. */
    const int length = lj92_segment_length(self, self->ix);
    if (!lj92_segment_fits(self, self->ix, length)) return LJ92_ERROR_CORRUPT;
    self->ix += length;
    return LJ92_ERROR_NONE;
}

#ifdef SLOW_HUFF
static int nextbit(ljp* self) {
    u32 b = self->b;
    if (self->cnt == 0) {
        /* r6 A2 class: past the payload the reader reads zero bytes, never memory after it. */
        u32 next = (self->ix >= 0 && self->ix < self->datalen) ? self->data[self->ix] : 0;
        b = next;
        /* r9 A1: the index stops at datalen (lj92_advance_entropy_ix), never past INT_MAX */
        self->ix = lj92_advance_entropy_ix(self->ix, next == 0xff ? 2 : 1, self->datalen);
        self->cnt = 8;
    }
    int bit = b >> 7;
    self->cnt--;
    self->b = (b << 1)&0xFF;
    return bit;
}

static int decode(ljp* self) {
    int i = 1;
    int code = nextbit(self);
    while (code > self->maxcode[i]) {
        i++;
        code = (code << 1) + nextbit(self);
    }
    int j = self->valptr[i];
    j = j + code - self->mincode[i];
    int value = self->huffval[j];
    return value;
}

static int receive(ljp* self,int ssss) {
    if (ssss == 16) {
        return 1 << 15;
    }
    int i = 0;
    int v = 0;
    while (i != ssss) {
        i++;
        v = (v<<1) + nextbit(self);
    }
    return v;
}

static int extend(ljp* self,int v,int t) {
    int vt = 1<<(t-1);
    if (v < vt) {
        vt = (-1 << t) + 1;
        v = v + vt;
    }
    return v;
}
#endif

static LJ92_ALWAYS_INLINE int nextdiff_fast(ljp* self, u32* b_io, int* cnt_io, int* ix_io) {
#ifdef SLOW_HUFF
    self->b = *b_io;
    self->cnt = *cnt_io;
    self->ix = *ix_io;
    int t = decode(self);
    int diff = receive(self,t);
    diff = extend(self,diff,t);
    *b_io = self->b;
    *cnt_io = self->cnt;
    *ix_io = self->ix;
#else
    u32 b = *b_io;
    int cnt = *cnt_io;
    int huffbits = self->huffbits;
    int ix = *ix_io;
    const u8* data = self->data;
    const u32* hufflut = self->hufflut;
    const int datalen = self->datalen;
    int next;
    while (cnt < huffbits) {
        next = lj92_load_entropy_pair(data, ix, datalen); /* r6 A1/A2: never past the payload */
        int one = next&0xFF;
        int two = next>>8;
        b = (b<<16)|(one<<8)|two;
        cnt += 16;
        ix = lj92_advance_entropy_ix(ix, 2, datalen); /* r9 A1: never past datalen */
        if (one==0xFF) {
            //printf("%x %x %x %x %d\n",one,two,b,b>>8,cnt);
            b >>= 8;
            cnt -= 8;
        } else if (two==0xFF) ix = lj92_advance_entropy_ix(ix, 1, datalen);
    }
    int index = b >> (cnt - huffbits);
    u32 huffentry = hufflut[index];
    if (huffentry & LJ92_HUFF_DIRECT_FLAG) {
        cnt -= (int)(huffentry & LJ92_HUFF_DIRECT_BITS_MASK);
        u32 keepbitsmask = cnt ? ((1u << cnt) - 1u) : 0u;
        *b_io = b & keepbitsmask;
        *cnt_io = cnt;
        *ix_io = ix;
        return lj92_unpack_huff_direct_diff(huffentry);
    }
    int usedbits = huffentry&0xFF;
    int t = huffentry>>8;
    //self->sssshist[t]++;
    cnt -= usedbits;
    int keepbitsmask = (1 << cnt)-1;
    b &= keepbitsmask;
    int diff;
    if (t == 16) {
        diff = 1 << 15;
    } else {
        while (cnt < t) {
            next = lj92_load_entropy_pair(data, ix, datalen); /* r6 A1/A2: never past the payload */
            int one = next&0xFF;
            int two = next>>8;
            b = (b<<16)|(one<<8)|two;
            cnt += 16;
            ix = lj92_advance_entropy_ix(ix, 2, datalen); /* r9 A1: never past datalen */
            if (one==0xFF) {
                b >>= 8;
                cnt -= 8;
            } else if (two==0xFF) ix = lj92_advance_entropy_ix(ix, 1, datalen);
        }
        cnt -= t;
        diff = b >> cnt;
        int vt = 1<<(t-1);
        if (diff < vt) {
            diff += 1 - (1 << t);
        }
    }
    keepbitsmask = (1 << cnt)-1;
    *b_io = b & keepbitsmask;
    *cnt_io = cnt;
    *ix_io = ix;
    //printf("%d %d\n",t,diff);
#ifdef DEBUG
#endif
#endif
    return diff;
}

static int lj92_pred6_split_enabled(void)
{
    static int initialized = 0;
    static int enabled = 0;
    if (!initialized) {
        const char* value = getenv("MLVAPP_PROFILE_LJ92_PRED6_SPLIT");
        enabled = (value != NULL
                   && value[0] != '\0'
                   && !(value[0] == '0' && value[1] == '\0'));
        initialized = 1;
    }
    return enabled;
}

static int lj92_generic_split_enabled(void)
{
    static int initialized = 0;
    static int enabled = 0;
    if (!initialized) {
        const char* value = getenv("MLVAPP_PROFILE_LJ92_GENERIC_SPLIT");
        enabled = (value != NULL
                   && value[0] != '\0'
                   && !(value[0] == '0' && value[1] == '\0'));
        initialized = 1;
    }
    return enabled;
}

static int lj92_pred1_fastpath_measurement_enabled(void)
{
    static int initialized = 0;
    static int enabled = 0;
    if (!initialized) {
        const char* value = getenv("MLVAPP_PRED1_FASTPATH_MEASUREMENT");
        enabled = (value != NULL
                   && value[0] != '\0'
                   && !(value[0] == '0' && value[1] == '\0'));
        initialized = 1;
    }
    return enabled;
}

static void lj92_reset_last_pred6_timing(void)
{
    g_lj92_last_pred6_split_active = 0;
    g_lj92_last_pred6_split_requested = 0;
    g_lj92_last_generic_split_active = 0;
    g_lj92_last_generic_split_requested = 0;
    g_lj92_last_pred1_fast_path_active = 0;
    g_lj92_last_pred1_fast_path_measurement_requested = 0;
    g_lj92_last_pred1_fast_path_measurement_active = 0;
    g_lj92_last_pred1_fast_path_eligible = 0;
    g_lj92_last_scan_component_count = 0;
    g_lj92_last_write_length = 0;
    g_lj92_last_expected_write_length = 0;
    g_lj92_last_skip_length = 0;
    g_lj92_last_linearize_active = 0;
    g_lj92_last_component_count = 0;
    g_lj92_last_predictor = -1;
    g_lj92_last_pred6_total_ms = 0.0;
    g_lj92_last_pred6_bitstream_ms = 0.0;
    g_lj92_last_pred6_predictor_ms = 0.0;
    g_lj92_last_generic_total_ms = 0.0;
    g_lj92_last_generic_bitstream_ms = 0.0;
    g_lj92_last_generic_predictor_ms = 0.0;
    g_lj92_last_pred1_fast_path_total_ms = 0.0;
    g_lj92_last_pred1_fast_path_bitstream_ms = 0.0;
    g_lj92_last_pred1_fast_path_predictor_ms = 0.0;
}

static void lj92_commit_last_pred6_timing(int active,
                                          double total_ms,
                                          double bitstream_ms,
                                          double predictor_ms)
{
    g_lj92_last_pred6_split_active = active;
    g_lj92_last_pred6_total_ms = total_ms;
    g_lj92_last_pred6_bitstream_ms = bitstream_ms;
    g_lj92_last_pred6_predictor_ms = predictor_ms;
}

static void lj92_commit_last_generic_timing(int active,
                                            double total_ms,
                                            double bitstream_ms,
                                            double predictor_ms)
{
    g_lj92_last_generic_split_active = active;
    g_lj92_last_generic_total_ms = total_ms;
    g_lj92_last_generic_bitstream_ms = bitstream_ms;
    g_lj92_last_generic_predictor_ms = predictor_ms;
}

static void lj92_commit_last_pred1_fast_path_timing(int fast_path_active,
                                                    int measurement_active,
                                                    double total_ms,
                                                    double bitstream_ms,
                                                    double predictor_ms)
{
    g_lj92_last_pred1_fast_path_active = fast_path_active;
    g_lj92_last_pred1_fast_path_measurement_active = measurement_active;
    g_lj92_last_pred1_fast_path_total_ms = total_ms;
    g_lj92_last_pred1_fast_path_bitstream_ms = bitstream_ms;
    g_lj92_last_pred1_fast_path_predictor_ms = predictor_ms;
}

static int lj92_can_use_pred1_fast_path(const ljp* self,
                                        int scan_component_count,
                                        int pred,
                                        int profile_generic_split)
{
    return !profile_generic_split
        && pred == 1
        && self->components > 0
        && scan_component_count == self->components
        && self->linearize == NULL
        && self->skiplen == 0
        && self->writelen == (self->x * self->y * self->components);
}

static int parseScanPred1Fast(ljp* self);

static int parseScanPred1FastMeasured(ljp* self)
{
    int ix = lj92_scan_entropy_start(self); /* r6 A2: bounded, was ix += BEH(data[scanstart]) */
    if (ix < 0) return LJ92_ERROR_CORRUPT;

    int cnt = 0;
    u32 b = 0;

    const int width = self->x;
    const int height = self->y;
    const int components = self->components;
    const int row_stride = width * components;
    const int base_prediction = 1 << (self->bits - 1);

    u16* out = self->image;
    u16* thisrow = self->outrow[0];
    u16* lastrow = self->outrow[1];
    int* diff_buffer = malloc((size_t)row_stride * sizeof(int));
    if (diff_buffer == NULL) {
        return parseScanPred1Fast(self);
    }

    const double total_start = mlv_stage_timing_now();
    double bitstream_ms = 0.0;
    double predictor_ms = 0.0;
    double stage_start;

    for (int row = 0; row < height; row++) {
        stage_start = mlv_stage_timing_now();
        for (int index = 0; index < row_stride; index++) {
            diff_buffer[index] = nextdiff_fast(self, &b, &cnt, &ix);
        }
        bitstream_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;

        stage_start = mlv_stage_timing_now();
        for (int col = 0; col < width; col++) {
            const int colx = col * components;
            const int prev_colx = (col - 1) * components;
            for (int component = 0; component < components; component++) {
                int Px;
                if ((row == 0) && (col == 0))
                    Px = base_prediction;
                else if (row == 0)
                    Px = thisrow[prev_colx + component];
                else if (col == 0)
                    Px = lastrow[component];
                else
                    Px = thisrow[prev_colx + component];

                const int value = (u16)(Px + diff_buffer[colx + component]);
                thisrow[colx + component] = (u16)value;
                out[colx + component] = (u16)value;
            }
        }
        predictor_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;

        u16* temprow = lastrow;
        lastrow = thisrow;
        thisrow = temprow;
        out += row_stride;
    }

    lj92_commit_last_pred1_fast_path_timing(1,
                                            1,
                                            (mlv_stage_timing_now() - total_start) * 1000.0,
                                            bitstream_ms,
                                            predictor_ms);
    free(diff_buffer);

    self->ix = ix;
    self->cnt = cnt;
    self->b = b;
    lj92_commit_last_generic_timing(0, 0.0, 0.0, 0.0);
    return LJ92_ERROR_NONE;
}

static int parseScanPred1Fast(ljp* self)
{
    int ix = lj92_scan_entropy_start(self); /* r6 A2: bounded, was ix += BEH(data[scanstart]) */
    if (ix < 0) return LJ92_ERROR_CORRUPT;

    int cnt = 0;
    u32 b = 0;

    const int width = self->x;
    const int height = self->y;
    const int components = self->components;
    const int row_stride = width * components;
    const int base_prediction = 1 << (self->bits - 1);

    u16* out = self->image;
    u16* thisrow = self->outrow[0];
    u16* lastrow = self->outrow[1];

    for (int row = 0; row < height; row++) {
        for (int col = 0; col < width; col++) {
            const int colx = col * components;
            const int prev_colx = (col - 1) * components;
            for (int component = 0; component < components; component++) {
                int Px;
                if ((row == 0) && (col == 0))
                    Px = base_prediction;
                else if (row == 0)
                    Px = thisrow[prev_colx + component];
                else if (col == 0)
                    Px = lastrow[component];
                else
                    Px = thisrow[prev_colx + component];

                const int value = (u16)(Px + nextdiff_fast(self, &b, &cnt, &ix));
                thisrow[colx + component] = (u16)value;
                out[colx + component] = (u16)value;
            }
        }

        u16* temprow = lastrow;
        lastrow = thisrow;
        thisrow = temprow;
        out += row_stride;
    }

    lj92_commit_last_pred1_fast_path_timing(1, 0, 0.0, 0.0, 0.0);
    self->ix = ix;
    self->cnt = cnt;
    self->b = b;
    lj92_commit_last_generic_timing(0, 0.0, 0.0, 0.0);
    return LJ92_ERROR_NONE;
}

static int parsePred6(ljp* self) {
    int ret = LJ92_ERROR_CORRUPT;
    const int profile_split = lj92_pred6_split_enabled();
    const double pred6_start = profile_split ? mlv_stage_timing_now() : 0.0;
    double bitstream_ms = 0.0;
    double predictor_ms = 0.0;
    double stage_start = 0.0;
    //int compcount = self->data[self->ix+2];
    int ix = lj92_scan_entropy_start(self); /* r6 A2: bounded, was ix += BEH(data[scanstart]) */
    if (ix < 0) return LJ92_ERROR_CORRUPT;
    int cnt = 0;
    u32 b = 0;
    int write = self->writelen;
    const int datalen = self->datalen;
    const int x = self->x;
    const int y = self->y;
    const int bits = self->bits;
    u16* const linearize = self->linearize;
    const int linlen = self->linlen;
    // Now need to decode huffman coded values
    int c = 0;
    int pixels = y * x;
    u16* out = self->image;
    u16* temprow;
    u16* thisrow = self->outrow[0];
    u16* lastrow = self->outrow[1];

    // First pixel predicted from base value
    int diff;
    int Px;
    int col = 0;
    //int row = 0;
    int left = 0;
    int linear;

    // First pixel
    if (profile_split) stage_start = mlv_stage_timing_now();
    diff = nextdiff_fast(self, &b, &cnt, &ix);
    if (profile_split) bitstream_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
    if (profile_split) stage_start = mlv_stage_timing_now();
    Px = 1 << (bits-1);
    left = Px + diff;
    left = (u16)left;
    if (linearize)
        linear = linearize[left];
    else
        linear = left;
    thisrow[col++] = left;
    out[c++] = linear;
    if (profile_split) predictor_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
    if (ix >= datalen) {
        self->ix = ix;
        self->cnt = cnt;
        self->b = b;
        if (profile_split)
            lj92_commit_last_pred6_timing(1, (mlv_stage_timing_now() - pred6_start) * 1000.0, bitstream_ms, predictor_ms);
        else
            lj92_commit_last_pred6_timing(0, 0.0, 0.0, 0.0);
        return ret;
    }
    --write;
    int rowcount = x-1;
    while (rowcount--) {
        if (profile_split) stage_start = mlv_stage_timing_now();
        diff = nextdiff_fast(self, &b, &cnt, &ix);
        if (profile_split) bitstream_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
        if (profile_split) stage_start = mlv_stage_timing_now();
        Px = left;
        left = Px + diff;
        left = (u16)left;
        if (linearize)
            linear = linearize[left];
        else
            linear = left;
        thisrow[col++] = left;
        out[c++] = linear;
        if (profile_split) predictor_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
        //printf("%d %d %d %d %x\n",col-1,diff,left,thisrow[col-1],&thisrow[col-1]);
        if (ix >= datalen) {
            self->ix = ix;
            self->cnt = cnt;
            self->b = b;
            if (profile_split)
                lj92_commit_last_pred6_timing(1, (mlv_stage_timing_now() - pred6_start) * 1000.0, bitstream_ms, predictor_ms);
            else
                lj92_commit_last_pred6_timing(0, 0.0, 0.0, 0.0);
            return ret;
        }
        if (--write==0) {
            out += self->skiplen;
            write = self->writelen;
        }
    }
    temprow = lastrow;
    lastrow = thisrow;
    thisrow = temprow;
    //row++;
    //printf("%x %x\n",thisrow,lastrow);
    while (c<pixels) {
        col = 0;
        if (profile_split) stage_start = mlv_stage_timing_now();
        diff = nextdiff_fast(self, &b, &cnt, &ix);
        if (profile_split) bitstream_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
        if (profile_split) stage_start = mlv_stage_timing_now();
        Px = lastrow[col]; // Use value above for first pixel in row
        left = Px + diff;
        left = (u16)left;
        if (linearize) {
            if (left>linlen) {
                self->ix = ix;
                self->cnt = cnt;
                self->b = b;
                if (profile_split)
                    lj92_commit_last_pred6_timing(1, (mlv_stage_timing_now() - pred6_start) * 1000.0, bitstream_ms, predictor_ms);
                else
                    lj92_commit_last_pred6_timing(0, 0.0, 0.0, 0.0);
                return LJ92_ERROR_CORRUPT;
            }
            linear = linearize[left];
        } else
            linear = left;
        thisrow[col++] = left;
        //printf("%d %d %d %d\n",col,diff,left,lastrow[col]);
        out[c++] = linear;
        if (profile_split) predictor_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
        if (ix >= datalen) break;
        rowcount = x-1;
        if (--write==0) {
            out += self->skiplen;
            write = self->writelen;
        }
        while (rowcount--) {
            if (profile_split) stage_start = mlv_stage_timing_now();
            diff = nextdiff_fast(self, &b, &cnt, &ix);
            if (profile_split) bitstream_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
            if (profile_split) stage_start = mlv_stage_timing_now();
            Px = lastrow[col] + ((left - lastrow[col-1])>>1);
            left = Px + diff;
            left = (u16)left;
            //printf("%d %d %d %d %d %x\n",col,diff,left,lastrow[col],lastrow[col-1],&lastrow[col]);
            if (linearize) {
                if (left>linlen) {
                    self->ix = ix;
                    self->cnt = cnt;
                    self->b = b;
                    if (profile_split)
                        lj92_commit_last_pred6_timing(1, (mlv_stage_timing_now() - pred6_start) * 1000.0, bitstream_ms, predictor_ms);
                    else
                        lj92_commit_last_pred6_timing(0, 0.0, 0.0, 0.0);
                    return LJ92_ERROR_CORRUPT;
                }
                linear = linearize[left];
            } else
                linear = left;
            thisrow[col++] = left;
            out[c++] = linear;
            if (profile_split) predictor_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
            if (--write==0) {
                out += self->skiplen;
                write = self->writelen;
            }
        }
        temprow = lastrow;
        lastrow = thisrow;
        thisrow = temprow;
        if (ix >= datalen) break;
    }
    self->ix = ix;
    self->cnt = cnt;
    self->b = b;
    if (profile_split)
        lj92_commit_last_pred6_timing(1, (mlv_stage_timing_now() - pred6_start) * 1000.0, bitstream_ms, predictor_ms);
    else
        lj92_commit_last_pred6_timing(0, 0.0, 0.0, 0.0);
    if (c >= pixels) ret = LJ92_ERROR_NONE;
    return ret;
}

static int parseScan(ljp* self) {
    int ret = LJ92_ERROR_CORRUPT;
    //memset(self->sssshist,0,sizeof(self->sssshist));
    self->ix = self->scanstart;
    /* r6 A2 class: lj92_open checked these header bytes; a handle that skipped it is refused, never read past. */
    if (self->scanstart < 0 || self->scanstart+2 >= self->datalen) return ret;
    int compcount = self->data[self->ix+2];
    if (3+2*compcount >= self->datalen - self->scanstart) return ret;
    int pred = self->data[self->ix+3+2*compcount];
    const int expected_write_length = self->x * self->y * self->components;
    g_lj92_last_pred6_split_requested = lj92_pred6_split_enabled();
    g_lj92_last_generic_split_requested = lj92_generic_split_enabled();
    g_lj92_last_pred1_fast_path_measurement_requested = lj92_pred1_fastpath_measurement_enabled();
    g_lj92_last_scan_component_count = compcount;
    g_lj92_last_write_length = self->writelen;
    g_lj92_last_expected_write_length = expected_write_length;
    g_lj92_last_skip_length = self->skiplen;
    g_lj92_last_linearize_active = self->linearize != NULL;
    g_lj92_last_component_count = self->components;
    g_lj92_last_predictor = pred;
    if (pred<0 || pred>7) return ret;
    g_lj92_last_pred1_fast_path_eligible =
        lj92_can_use_pred1_fast_path(self,
                                     compcount,
                                     pred,
                                     g_lj92_last_generic_split_requested);
    /* r6 pass 1 A1 (adversary on be8e2c33): parsePred6 decodes x * y samples, one component per column, so a
     * multi-component pred-6 scan returned success with x * y * (components - 1) samples never written. It takes
     * the generic scan below, which writes every component of every column. */
    if (pred==6 && self->components == 1) return parsePred6(self); // Fast path
    if (g_lj92_last_pred1_fast_path_eligible)
        return g_lj92_last_pred1_fast_path_measurement_requested
            ? parseScanPred1FastMeasured(self)
            : parseScanPred1Fast(self);
    const int profile_generic_split = g_lj92_last_generic_split_requested;
    const double generic_start = profile_generic_split ? mlv_stage_timing_now() : 0.0;
    double generic_bitstream_ms = 0.0;
    double generic_predictor_ms = 0.0;
    double stage_start = 0.0;
    int ix = lj92_scan_entropy_start(self); /* r6 A2: bounded, was ix += BEH(data[scanstart]) */
    if (ix < 0) return LJ92_ERROR_CORRUPT;
    int cnt = 0;
    u32 b = 0;
    u16* out = self->image;
    u16* thisrow = self->outrow[0];
    u16* lastrow = self->outrow[1];

    // First pixel predicted from base value
    int diff;
    int Px = 0;
    int left = 0;
    for (int row = 0; row < self->y; row++) {
        for (int col = 0; col < self->x; col++) {
            int colx = col * self->components;
            for (int c = 0; c < self->components; c++) {
                if (profile_generic_split) stage_start = mlv_stage_timing_now();
                if ((col==0)&&(row==0)) {
                    Px = 1 << (self->bits-1);
                } else if (row==0) {
                    // Px = left;
                    Px = thisrow[(col - 1) * self->components + c];
                } else if (col==0) {
                    Px = lastrow[c];  // Use value above for first pixel in row
                } else {
                    int prev_colx = (col - 1) * self->components;
                    /* r6 pass 2 A1 (adversary on f1659d6a): Ra is the same component's left neighbour. The scalar
                     * `left` is the previous sample of ANY component, which is Ra only when components == 1 (it then
                     * equals thisrow[prev_colx + c], so one-component pictures are unchanged). */
                    const int Ra = thisrow[prev_colx + c];

                    switch (pred) {
                        case 0:
                          Px = 0;
                          break;  // No prediction... should not be used
                        case 1:
                          Px = thisrow[prev_colx + c];
                          break;
                        case 2:
                          Px = lastrow[colx + c];
                          break;
                        case 3:
                          Px = lastrow[prev_colx + c];
                          break;
                        case 4:
                          Px = Ra + lastrow[colx + c] - lastrow[prev_colx + c];
                          break;
                        case 5:
                          Px = Ra + ((lastrow[colx + c] - lastrow[prev_colx + c]) >> 1);
                          break;
                        case 6:
                          Px = lastrow[colx + c] + ((Ra - lastrow[prev_colx + c]) >> 1);
                          break;
                        case 7:
                          Px = (Ra + lastrow[colx + c]) >> 1;
                          break;
                    }
                }
                if (profile_generic_split) generic_predictor_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
                if (profile_generic_split) stage_start = mlv_stage_timing_now();
                diff = nextdiff_fast(self, &b, &cnt, &ix);
                if (profile_generic_split) generic_bitstream_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
                if (profile_generic_split) stage_start = mlv_stage_timing_now();
                left = Px + diff;
                left = (u16)left;
                //printf("%d %d %d\n",c,diff,left);
                int linear;
                if (self->linearize) {
                    if (left>self->linlen) {
                        self->ix = ix;
                        self->cnt = cnt;
                        self->b = b;
                        if (profile_generic_split)
                            lj92_commit_last_generic_timing(1,
                                                            (mlv_stage_timing_now() - generic_start) * 1000.0,
                                                            generic_bitstream_ms,
                                                            generic_predictor_ms);
                        else
                            lj92_commit_last_generic_timing(0, 0.0, 0.0, 0.0);
                        return LJ92_ERROR_CORRUPT;
                    }
                    linear = self->linearize[left];
                } else
                    linear = left;

                thisrow[colx + c] = left;
                out[colx + c] = linear; // HACK
                if (profile_generic_split) generic_predictor_ms += (mlv_stage_timing_now() - stage_start) * 1000.0;
            } // c
        } // col

        u16* temprow = lastrow;
        lastrow = thisrow;
        thisrow = temprow;

        out += self->x * self->components + self->skiplen;
    } // row

    self->ix = ix;
    self->cnt = cnt;
    self->b = b;

    if (profile_generic_split)
        lj92_commit_last_generic_timing(1,
                                        (mlv_stage_timing_now() - generic_start) * 1000.0,
                                        generic_bitstream_ms,
                                        generic_predictor_ms);
    else
        lj92_commit_last_generic_timing(0, 0.0, 0.0, 0.0);
    ret = LJ92_ERROR_NONE;
    return ret;
}

static int parseImage(ljp* self) {
    int ret = LJ92_ERROR_NONE;
    while (1) {
        int nextMarker = find(self);
        if (nextMarker == 0xc4)
            ret = parseHuff(self);
        else if (nextMarker == 0xc3)
            ret = parseSof3(self);
        else if (nextMarker == 0xfe)// Comment
            ret = parseBlock(self);
        else if (nextMarker == 0xd9) // End of image
            break;
        else if (nextMarker == 0xda) {
            self->scanstart = self->ix;
            /* r6 (adversary r5 pass 3 A2): an SOS whose header runs past the payload (FF FF as its length in
             * a 112-byte frame) is refused here, so lj92_open never reports a scan the bit reader would start
             * outside the buffer. The header bytes parseScan reads (component count, predictor) are inside it. */
            const int entropy = lj92_scan_entropy_start(self);
            const int headerok = entropy >= 0 && self->scanstart+2 < self->datalen;
            const int compcount = headerok ? self->data[self->scanstart+2] : 0;
            if (!headerok || 3+2*compcount >= self->datalen - self->scanstart) {
                ret = LJ92_ERROR_CORRUPT;
                break;
            }
            ret = LJ92_ERROR_NONE;
            break;
        } else if (nextMarker == -1) {
            ret = LJ92_ERROR_CORRUPT;
            break;
        } else
            ret = parseBlock(self);
        if (ret != LJ92_ERROR_NONE) break;
    }
    return ret;
}

static int findSoI(ljp* self) {
    int ret = LJ92_ERROR_CORRUPT;
    if (find(self)==0xd8)
        ret = parseImage(self);
    return ret;
}

static void free_memory(ljp* self) {
#ifdef SLOW_HUFF
    free(self->maxcode);
    self->maxcode = NULL;
    free(self->mincode);
    self->mincode = NULL;
    free(self->valptr);
    self->valptr = NULL;
    free(self->huffval);
    self->huffval = NULL;
    free(self->huffsize);
    self->huffsize = NULL;
    free(self->huffcode);
    self->huffcode = NULL;
#else
    free(self->hufflut);
    self->hufflut = NULL;
#endif
    free(self->rowcache);
    self->rowcache = NULL;
}

int lj92_open(lj92* lj,
              uint8_t* data, int datalen,
              int* width,int* height, int* bitdepth, int* components) {
    ljp* self = (ljp*)calloc(sizeof(ljp),1);
    if (self==NULL) return LJ92_ERROR_NO_MEMORY;

    self->data = (u8*)data;
    self->dataend = self->data + datalen;
    self->datalen = datalen;

    int ret = findSoI(self);

    /* r6 pass 1 A2 (advisory, pre-existing): a scan with no Huffman table before it decoded through a NULL lookup
     * table. It can decode nothing, so it is refused. */
#ifdef SLOW_HUFF
    if (ret == LJ92_ERROR_NONE && self->maxcode == NULL) ret = LJ92_ERROR_CORRUPT;
#else
    if (ret == LJ92_ERROR_NONE && self->hufflut == NULL) ret = LJ92_ERROR_CORRUPT;
#endif

    if (ret == LJ92_ERROR_NONE) {
        if (self->x <= 0 || self->components <= 0
            || (size_t)self->x > SIZE_MAX / (size_t)self->components) {
            ret = LJ92_ERROR_NO_MEMORY;
        }
    }

    if (ret == LJ92_ERROR_NONE) {
        const size_t row_elements = (size_t)self->x * (size_t)self->components;
        if (row_elements > SIZE_MAX / (2u * sizeof(u16))) {
            ret = LJ92_ERROR_NO_MEMORY;
        }
        u16* rowcache = ret == LJ92_ERROR_NONE
            ? (u16*)calloc(row_elements, 2u * sizeof(u16))
            : NULL;
        if (rowcache == NULL) ret = LJ92_ERROR_NO_MEMORY;
        else {
            self->rowcache = rowcache;
            self->outrow[0] = rowcache;
            /* r6 pass 2 A2 (advisory, pre-existing): a row holds x * components samples, so the second row starts
             * there; at x it overlapped the first row whenever components > 1 (one component: unchanged). */
            self->outrow[1] = &rowcache[row_elements];
        }
    }

    if (ret != LJ92_ERROR_NONE) { // Failed, clean up
        *lj = NULL;
        free_memory(self);
        free(self);
    } else {
        *width = self->x;
        *height = self->y;
        *bitdepth = self->bits;
        *components = self->components;
        *lj = self;
    }
    return ret;
}

int lj92_decode(lj92 lj,
                uint16_t* target,int writeLength, int skipLength,
                uint16_t* linearize,int linearizeLength) {
    int ret = LJ92_ERROR_NONE;
    ljp* self = lj;
    if (self == NULL) return LJ92_ERROR_BAD_HANDLE;
    lj92_reset_last_pred6_timing();
    self->image = target;
    self->writelen = writeLength;
    self->skiplen = skipLength;
    self->linearize = linearize;
    self->linlen = linearizeLength;
    ret = parseScan(self);
    return ret;
}

int lj92_get_last_pred6_split_active(void) {
    return g_lj92_last_pred6_split_active;
}

int lj92_get_last_pred6_split_requested(void) {
    return g_lj92_last_pred6_split_requested;
}

int lj92_get_last_generic_split_active(void) {
    return g_lj92_last_generic_split_active;
}

int lj92_get_last_generic_split_requested(void) {
    return g_lj92_last_generic_split_requested;
}

int lj92_get_last_pred1_fast_path_active(void) {
    return g_lj92_last_pred1_fast_path_active;
}

int lj92_get_last_pred1_fast_path_measurement_requested(void) {
    return g_lj92_last_pred1_fast_path_measurement_requested;
}

int lj92_get_last_pred1_fast_path_measurement_active(void) {
    return g_lj92_last_pred1_fast_path_measurement_active;
}

int lj92_get_last_pred1_fast_path_eligible(void) {
    return g_lj92_last_pred1_fast_path_eligible;
}

int lj92_get_last_scan_component_count(void) {
    return g_lj92_last_scan_component_count;
}

int lj92_get_last_write_length(void) {
    return g_lj92_last_write_length;
}

int lj92_get_last_expected_write_length(void) {
    return g_lj92_last_expected_write_length;
}

int lj92_get_last_skip_length(void) {
    return g_lj92_last_skip_length;
}

int lj92_get_last_linearize_active(void) {
    return g_lj92_last_linearize_active;
}

int lj92_get_last_component_count(void) {
    return g_lj92_last_component_count;
}

int lj92_get_last_predictor(void) {
    return g_lj92_last_predictor;
}

double lj92_get_last_pred6_total_ms(void) {
    return g_lj92_last_pred6_total_ms;
}

double lj92_get_last_pred6_bitstream_ms(void) {
    return g_lj92_last_pred6_bitstream_ms;
}

double lj92_get_last_pred6_predictor_ms(void) {
    return g_lj92_last_pred6_predictor_ms;
}

double lj92_get_last_generic_total_ms(void) {
    return g_lj92_last_generic_total_ms;
}

double lj92_get_last_generic_bitstream_ms(void) {
    return g_lj92_last_generic_bitstream_ms;
}

double lj92_get_last_generic_predictor_ms(void) {
    return g_lj92_last_generic_predictor_ms;
}

double lj92_get_last_pred1_fast_path_total_ms(void) {
    return g_lj92_last_pred1_fast_path_total_ms;
}

double lj92_get_last_pred1_fast_path_bitstream_ms(void) {
    return g_lj92_last_pred1_fast_path_bitstream_ms;
}

double lj92_get_last_pred1_fast_path_predictor_ms(void) {
    return g_lj92_last_pred1_fast_path_predictor_ms;
}

void lj92_close(lj92 lj) {
    ljp* self = lj;
    if (self != NULL)
        free_memory(self);
    free(self);
}

/* Encoder implementation */

typedef struct _lje {
    uint16_t* image;
    int width;
    int height;
    int bitdepth;
    int readLength;
    int skipLength;
    uint16_t* delinearize;
    int delinearizeLength;
    uint8_t* encoded;
    int encodedWritten;
    int encodedLength;
    int hist[18]; // SSSS frequency histogram
    int bits[18];
    int huffval[18];
    u16 huffenc[18];
    u16 huffbits[18];
    int huffsym[18];
} lje;

int frequencyScan(lje* self) {
    // Scan through the tile using the standard type 6 prediction
    // Need to cache the previous 2 row in target coordinates because of tiling
    uint16_t* pixel = self->image;
    int pixcount = self->width * self->height;
    int scan = self->readLength;
    uint16_t* rowcache = (uint16_t*)calloc((size_t)self->width * 2u, sizeof(uint16_t));
    if (rowcache == NULL) return LJ92_ERROR_NO_MEMORY;
    uint16_t* rows[2];
    rows[0] = rowcache;
    rows[1] = &rowcache[self->width];

    int col = 0;
    int row = 0;
    int Px = 0;
    int32_t diff = 0;
    //int maxval = (1 << self->bitdepth);
    while (pixcount--) {
        uint16_t p = *pixel;
        /* if (self->delinearize) {
            if (p>=self->delinearizeLength) {
                free(rowcache);
                return LJ92_ERROR_TOO_WIDE;
            }
            p = self->delinearize[p];
        }
        if (p>=maxval) {
            free(rowcache);
            return LJ92_ERROR_TOO_WIDE;
        } */
        rows[1][col] = p;

        if ((row == 0)&&(col == 0))
            Px = 1 << (self->bitdepth-1);
        else if (row == 0)
            Px = rows[1][col-1];
        else if (col == 0)
            Px = rows[0][col];
        else
            Px = rows[0][col] + ((rows[1][col-1] - rows[0][col-1])>>1);
        diff = rows[1][col] - Px;
        diff = diff%65536;
        diff = (int16_t)diff;
        int ssss = 32 - __builtin_clz(abs(diff));
        if (diff==0) ssss=0;
        self->hist[ssss]++;
        //printf("%d %d %d %d %d %d\n",col,row,p,Px,diff,ssss);
        pixel++;
        scan--;
        col++;
        if (scan==0) { pixel += self->skipLength; scan = self->readLength; }
        if (col==self->width) {
            uint16_t* tmprow = rows[1];
            rows[1] = rows[0];
            rows[0] = tmprow;
            col=0;
            row++;
        }
    }
#ifdef DEBUG
    int sort[17];
    for (int h=0;h<17;h++) {
        sort[h] = h;
        printf("%d:%d\n",h,self->hist[h]);
    }
#endif
    free(rowcache);
    return LJ92_ERROR_NONE;
}

void createEncodeTable(lje* self) {
    float freq[18];
    int codesize[18];
    int others[18];

    // Calculate frequencies
    float totalpixels = self->width * self->height;
    for (int i=0;i<17;i++) {
        freq[i] = (float)(self->hist[i])/totalpixels;
#ifdef DEBUG
        printf("%d:%f\n",i,freq[i]);
#endif
        codesize[i] = 0;
        others[i] = -1;
    }
    codesize[17] = 0;
    others[17] = -1;
    freq[17] = 1.0f;

    float v1f,v2f;
    int v1,v2;

    while (1) {
        v1f=3.0f;
        v1=-1;
        for (int i=0;i<18;i++) {
            if ((freq[i]<=v1f) && (freq[i]>0.0f)) {
                v1f = freq[i];
                v1 = i;
            }
        }
#ifdef DEBUG
        printf("v1:%d,%f\n",v1,v1f);
#endif
        v2f=3.0f;
        v2=-1;
        for (int i=0;i<18;i++) {
            if (i==v1) continue;
            if ((freq[i]<v2f) && (freq[i]>0.0f)) {
                v2f = freq[i];
                v2 = i;
            }
        }
        if (v2==-1) break; // Done

        freq[v1] += freq[v2];
        freq[v2] = 0.0f;

        while (1) {
            codesize[v1]++;
            if (others[v1]==-1) break;
            v1 = others[v1];
        }
        others[v1] = v2;
        while (1) {
            codesize[v2]++;
            if (others[v2]==-1) break;
            v2 = others[v2];
        }
    }
    int* bits = self->bits;
    memset(bits,0,sizeof(self->bits));
    for (int i=0;i<18;i++) {
        if (codesize[i]!=0) {
            bits[codesize[i]]++;
        }
    }
#ifdef DEBUG
    for (int i=0;i<18;i++) {
        printf("bits:%d,%d,%d\n",i,bits[i],codesize[i]);
    }
#endif
    //adjust bits, this step is a must to remove a code with all ones
    //and fix bug of overriding SSSS-0 category with the code with all ones.
    int I = 17;
    while(1) {
        if(bits[I] > 0) {
            int J = I - 1;
            do {
                J = J - 1;
            } while(bits[J] <= 0);
            bits[I] = bits[I] - 2;
            bits[I - 1] = bits[I - 1] + 1; 
            bits[J + 1] = bits[J + 1] + 2;
            bits[J] = bits [J] - 1;
        } else {
            I = I - 1;
            if(I != 16) {
                continue;
            }
            while(bits[I] == 0) {
                I = I - 1;
            }
            bits[I] = bits[I] - 1;
            break;
        }  
    }
#ifdef DEBUG
for (int i=0;i<18;i++) {
    printf("Adjusted bits:%d,%d,%d\n",i,bits[i],codesize[i]);
}
#endif
    int* huffval = self->huffval;
    int i=1;
    int k=0;
    int j;
    memset(huffval,0,sizeof(self->huffval));
    while (i<=32) {
        j=0;
        while (j<17) {
            if (codesize[j]==i) {
                huffval[k++] = j;
            }
            j++;
        }
        i++;
    }
#ifdef DEBUG
    for (i=0;i<18;i++) {
        printf("i=%d,huffval[i]=%x\n",i,huffval[i]);
    }
#endif
    int maxbits = 16;
    while (maxbits>0) {
        if (bits[maxbits]) break;
        maxbits--;
    }
    u16* huffenc = self->huffenc;
    u16* huffbits = self->huffbits;
    int* huffsym = self->huffsym;
    memset(huffenc,0,sizeof(self->huffenc));
    memset(huffbits,0,sizeof(self->huffbits));
    memset(self->huffsym,0,sizeof(self->huffsym));
    i = 0;
    //int hv = 0;
    int rv = 0;
    int vl = 0; // i
    //int hcode;
    int bitsused = 1;
    int sym = 0;
    //printf("%04x:%x:%d:%x\n",i,huffvals[hv],bitsused,1<<(maxbits-bitsused));
    while (i<1<<maxbits) {
        if (bitsused>maxbits) {
            break; // Done. Should never get here!
        }
        if (vl >= bits[bitsused]) {
            bitsused++;
            vl = 0;
            continue;
        }
        if (rv == 1 << (maxbits-bitsused)) {
            rv = 0;
            vl++;
            //hv++;
            //printf("%04x:%x:%d:%x\n",i,huffvals[hv],bitsused,1<<(maxbits-bitsused));
            continue;
        }
        huffbits[sym] = bitsused;
        huffenc[sym++] = i>>(maxbits-bitsused);
        //printf("%d %d %d\n",i,bitsused,hcode);
        i+= (1<<(maxbits-bitsused));
        rv = 1<<(maxbits-bitsused);
    }
    for (i=0;i<18;i++) {
        if (huffbits[i]>0) {
            huffsym[huffval[i]] = i;
        }
#ifdef DEBUG
        printf("huffval[%d]=%d,huffenc[%d]=%x,bits=%d\n",i,huffval[i],i,huffenc[i],huffbits[i]);
#endif
        if (huffbits[i]>0) {
            huffsym[huffval[i]] = i;
        }
    }
#ifdef DEBUG
    for (i=0;i<18;i++) {
        printf("huffsym[%d]=%d\n",i,huffsym[i]);
    }
#endif
}

void writeHeader(lje* self) {
    int w = self->encodedWritten;
    uint8_t* e = self->encoded;
    e[w++] = 0xff; e[w++] = 0xd8; //SOI
    e[w++] = 0xff; e[w++] = 0xc4; //HUFF
    // Write HUFF
        int count = 0;
        for (int i=0;i<17;i++) {
            count += self->bits[i];
        }
        e[w++] = 0x0; e[w++] = 17+2+count; //Lf, frame header length
        e[w++] = 0; // Table ID
        for (int i=1;i<17;i++) {
            e[w++] = self->bits[i];
        }
        for (int i=0;i<count;i++) {
            e[w++] = self->huffval[i];
        }
    e[w++] = 0xff; e[w++] = 0xc3; //SOF3
        // Write SOF
        e[w++] = 0x0; e[w++] = 11; //Lf, frame header length
        e[w++] = self->bitdepth;
        e[w++] = self->height>>8; e[w++] = self->height&0xFF;
        e[w++] = self->width>>8; e[w++] = self->width&0xFF;
        e[w++] = 1; // Components
        e[w++] = 0; // Component ID
        e[w++] = 0x11; // Component X/Y
        e[w++] = 0; // Unused (Quantisation)
    e[w++] = 0xff; e[w++] = 0xda; //SCAN
    // Write SCAN
        e[w++] = 0x0; e[w++] = 8; //Ls, scan header length
        e[w++] = 1; // Components
        e[w++] = 0; //
        e[w++] = 0; //
        e[w++] = 6; // Predictor
        e[w++] = 0; //
        e[w++] = 0; //
    self->encodedWritten = w;
}

void writePost(lje* self) {
    int w = self->encodedWritten;
    uint8_t* e = self->encoded;
    e[w++] = 0xff; e[w++] = 0xd9; //EOI
    self->encodedWritten = w;
}

int writeBody(lje* self) {
    // Scan through the tile using the standard type 6 prediction
    // Need to cache the previous 2 row in target coordinates because of tiling
    uint16_t* pixel = self->image;
    int pixcount = self->width * self->height;
    int scan = self->readLength;
    uint16_t* rowcache = (uint16_t*)calloc((size_t)self->width * 2u, sizeof(uint16_t));
    if (rowcache == NULL) return LJ92_ERROR_NO_MEMORY;
    uint16_t* rows[2];
    rows[0] = rowcache;
    rows[1] = &rowcache[self->width];

    int col = 0;
    int row = 0;
    int Px = 0;
    int32_t diff = 0;
    //int bitcount = 0;
    uint8_t* out = self->encoded;
    int w = self->encodedWritten;
    uint8_t next = 0;
    uint8_t nextbits = 8;
    while (pixcount--) {
        uint16_t p = *pixel;
        if (self->delinearize) p = self->delinearize[p];
        rows[1][col] = p;

        if ((row == 0)&&(col == 0))
            Px = 1 << (self->bitdepth-1);
        else if (row == 0)
            Px = rows[1][col-1];
        else if (col == 0)
            Px = rows[0][col];
        else
            Px = rows[0][col] + ((rows[1][col-1] - rows[0][col-1])>>1);
        diff = rows[1][col] - Px;
        diff = diff%65536;
        diff = (int16_t)diff;
        int ssss = 32 - __builtin_clz(abs(diff));
        if (diff==0) ssss=0;
        //printf("%d %d %d %d %d\n",col,row,Px,diff,ssss);

        // Write the huffman code for the ssss value
        int huffcode = self->huffsym[ssss];
        int huffenc = self->huffenc[huffcode];
        int huffbits = self->huffbits[huffcode];

        int vt = ssss>0?(1<<(ssss-1)):0;
        //printf("%d %d %d %d\n",rows[1][col],Px,diff,Px+diff);
#ifdef DEBUG
#endif
        if (diff < vt)
            diff += (1 << (ssss))-1;

        // Write the ssss
        while (huffbits>0) {
            int usebits = huffbits>nextbits?nextbits:huffbits;
            // Add top usebits from huffval to next usebits of nextbits
            int tophuff = huffenc >> (huffbits - usebits);
            next |= (tophuff << (nextbits-usebits));
            nextbits -= usebits;
            huffbits -= usebits;
            huffenc &= (1<<huffbits)-1;
            if (nextbits==0) {
                if(w >= self->encodedLength - 1)
                {
                    free(rowcache);
                    return LJ92_ERROR_ENCODER;
                }
                out[w++] = next;
                if (next==0xff) out[w++] = 0x0;
                next = 0;
                nextbits = 8;
            }
        }
        // Write the rest of the bits for the value
        if (ssss == 16) {
            // Diff values (always 32678) for SSSS=16 are encoded with 0 bits
            ssss = 0;
        }
        while (ssss>0) {
            int usebits = ssss>nextbits?nextbits:ssss;
            // Add top usebits from huffval to next usebits of nextbits
            int tophuff = diff >> (ssss - usebits);
            next |= (tophuff << (nextbits-usebits));
            nextbits -= usebits;
            ssss -= usebits;
            diff &= (1<<ssss)-1;
            if (nextbits==0) {
                if(w >= self->encodedLength - 1)
                {
                    free(rowcache);
                    return LJ92_ERROR_ENCODER;
                }
                out[w++] = next;
                if (next==0xff) out[w++] = 0x0;
                next = 0;
                nextbits = 8;
            }
        }

        //printf("%d %d\n",diff,ssss);
        pixel++;
        scan--;
        col++;
        if (scan==0) { pixel += self->skipLength; scan = self->readLength; }
        if (col==self->width) {
            uint16_t* tmprow = rows[1];
            rows[1] = rows[0];
            rows[0] = tmprow;
            col=0;
            row++;
        }
    }
    // Flush the final bits
    if (nextbits<8) {
        out[w++] = next;
        if (next==0xff) out[w++] = 0x0;
    }
#ifdef DEBUG
    int sort[18];
    for (int h=0;h<18;h++) {
        sort[h] = h;
        printf("%d:%d\n",h,self->hist[h]);
    }
    printf("Total bytes: %d\n",bitcount>>3);
#endif
    free(rowcache);
    self->encodedWritten = w;
    return LJ92_ERROR_NONE;
}
/* Encoder
 * Read tile from an image and encode in one shot
 * Return the encoded data
 */
int lj92_encode(uint16_t* image, int width, int height, int bitdepth,
                int readLength, int skipLength,
                uint16_t* delinearize,int delinearizeLength,
                uint8_t** encoded, int* encodedLength) {
    int ret = LJ92_ERROR_NONE;
    if (!image || !encoded || !encodedLength || width <= 0 || height <= 0
        || width > (INT_MAX - 200) / 3 / height) {
        return LJ92_ERROR_CORRUPT;
    }
    *encoded = NULL;
    *encodedLength = 0;

    lje* self = (lje*)calloc(sizeof(lje),1);
    if (self==NULL) return LJ92_ERROR_NO_MEMORY;
    self->image = image;
    self->width = width;
    self->height = height;
    self->bitdepth = bitdepth;
    self->readLength = readLength;
    self->skipLength = skipLength;
    self->delinearize = delinearize;
    self->delinearizeLength = delinearizeLength;
    self->encodedLength = width * height * 3 + 200;
    self->encoded = malloc(self->encodedLength);
    if (self->encoded==NULL) { free(self); return LJ92_ERROR_NO_MEMORY; }
    // Scan through data to gather frequencies of ssss prefixes
    ret = frequencyScan(self);
    if (ret != LJ92_ERROR_NONE) {
        free(self->encoded);
        free(self);
        return ret;
    }
    // Create encoded table based on frequencies
    createEncodeTable(self);
    // Write JPEG head and scan header
    writeHeader(self);
    // Scan through and do the compression
    ret = writeBody(self);
    if (ret != LJ92_ERROR_NONE) {
        free(self->encoded);
        free(self);
        return ret;
    }
    // Finish
    writePost(self);
#ifdef DEBUG
    printf("written:%d\n",self->encodedWritten);
#endif
    uint8_t *shrunk = realloc(self->encoded, self->encodedWritten);
    if (shrunk != NULL) {
        self->encoded = shrunk;
    }
    self->encodedLength = self->encodedWritten;
    *encoded = self->encoded;
    *encodedLength = self->encodedLength;

    free(self);

    return ret;
}

