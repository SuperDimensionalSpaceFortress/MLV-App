/*
MIT License

Copyright (c) 2017 Ming

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Fixed bugs and adapted to RGB64 by masc4ii (c) 2019
Added dual threading by masc4ii (c) 2021
*/

#include "RBFilterPlain.h"
#include <algorithm>
#include <cstring>
#include <math.h>
#include <omp.h>

using namespace std;

#if defined(__GNUC__) || defined(__clang__)
#ifndef LIKELY
#define LIKELY(x) __builtin_expect(!!(x), 1)
#endif
#else
#ifndef LIKELY
#define LIKELY(x) (x)
#endif
#endif

#define QX_DEF_U16_MAX 65535

static inline int getDiffFactorRgb3(const uint16_t *color1, const uint16_t *color2)
{
    const int dr = abs((int)color1[0] - (int)color2[0]);
    const int dg = abs((int)color1[1] - (int)color2[1]);
    const int db = abs((int)color1[2] - (int)color2[2]);
    return ((dr + db) >> 2) + (dg >> 1);
}

static inline uint16_t limitU16FromInt32(int32_t value)
{
    if (value < 0) return 0;
    if (value > QX_DEF_U16_MAX) return QX_DEF_U16_MAX;
    return static_cast<uint16_t>(value);
}

CRBFilterPlain::CRBFilterPlain()
{

}

CRBFilterPlain::~CRBFilterPlain()
{
	releaseMemory();
}

// assumes 3/4 channel images, 1 byte per channel
void CRBFilterPlain::reserveMemory(int max_width, int max_height, int channels)
{
	// basic sanity check
    if(!(max_width >= 10 && max_width < 10000))return;
    if(!(max_height >= 10 && max_height < 10000))return;
    if(!(channels >= 1 && channels <= 4))return;

	if (m_left_pass_color
        && m_left_pass_factor
        && m_right_pass_color
        && m_right_pass_factor
        && m_down_pass_color
        && m_down_pass_factor
        && m_up_pass_color
        && m_up_pass_factor
        && m_reserve_channels == channels
        && m_reserve_width >= max_width
        && m_reserve_height >= max_height)
    {
        return;
    }

	releaseMemory();

	m_reserve_width = max_width;
	m_reserve_height = max_height;
	m_reserve_channels = channels;

	int width_height = m_reserve_width * m_reserve_height;
	int width_height_channel = width_height * m_reserve_channels;

	m_left_pass_color = new float[width_height_channel]();
	m_left_pass_factor = new float[width_height]();

	m_right_pass_color = new float[width_height_channel]();
	m_right_pass_factor = new float[width_height]();

	m_down_pass_color = new float[width_height_channel]();
	m_down_pass_factor = new float[width_height]();

	m_up_pass_color = new float[width_height_channel]();
	m_up_pass_factor = new float[width_height]();
}

void CRBFilterPlain::releaseMemory()
{
	m_reserve_width = 0;
	m_reserve_height = 0;
	m_reserve_channels = 0;

	if (m_left_pass_color)
	{
		delete[] m_left_pass_color;
		m_left_pass_color = nullptr;
	}

	if (m_left_pass_factor)
	{
		delete[] m_left_pass_factor;
		m_left_pass_factor = nullptr;
	}

	if (m_right_pass_color)
	{
		delete[] m_right_pass_color;
		m_right_pass_color = nullptr;
	}

	if (m_right_pass_factor)
	{
		delete[] m_right_pass_factor;
		m_right_pass_factor = nullptr;
	}

	if (m_down_pass_color)
	{
		delete[] m_down_pass_color;
		m_down_pass_color = nullptr;
	}

	if (m_down_pass_factor)
	{
		delete[] m_down_pass_factor;
		m_down_pass_factor = nullptr;
	}

	if (m_up_pass_color)
	{
		delete[] m_up_pass_color;
		m_up_pass_color = nullptr;
	}

	if (m_up_pass_factor)
	{
		delete[] m_up_pass_factor;
		m_up_pass_factor = nullptr;
	}

	if (m_range_table)
	{
		delete[] m_range_table;
		m_range_table = nullptr;
	}
	m_range_table_valid = false;
	m_range_table_alpha = 0.0f;
	m_range_table_inv_sigma = 0.0f;
}

int CRBFilterPlain::getDiffFactor(const uint16_t *color1, const uint16_t *color2) const
{
	int final_diff;
	int component_diff[4];

	// find absolute difference between each component
	for (int i = 0; i < m_reserve_channels; i++)
	{
        component_diff[i] = abs((int)color1[i] - (int)color2[i]);
	}

    // based on number of components, produce a single difference value in the 0-QX_DEF_U16_MAX range
	switch (m_reserve_channels)
	{
	case 1:
		final_diff = component_diff[0];
		break;

	case 2:
		final_diff = ((component_diff[0] + component_diff[1]) >> 1);
		break;

	case 3:
		final_diff = ((component_diff[0] + component_diff[2]) >> 2) + (component_diff[1] >> 1);
		break;

	case 4:
		final_diff = ((component_diff[0] + component_diff[1] + component_diff[2] + component_diff[3]) >> 2);
		break;

	default:
		final_diff = 0;
	}

    if( final_diff < 0 ) final_diff = 0;
    if( final_diff > QX_DEF_U16_MAX ) final_diff = QX_DEF_U16_MAX;
    //if(!(final_diff >= 0 && final_diff <= QX_DEF_U16_MAX))return 1;

	return final_diff;
}

// Column-parallel form of the legacy vertical passes in filter().
// After their first line the legacy passes advance width-1 pixels per row, so in
// linear pixel order element k depends only on element k-width (down) or
// k+width (up). Each residue class k mod width is therefore an independent
// chain. A worker owns the columns [column_begin, column_end) of the width-wide
// view of the buffers and walks them row by row. Every element is computed from
// the same operands with the same expression shape as the legacy loop (so any
// compiler contraction is the same too), which keeps the result bit-exact.
void CRBFilterPlain::verticalDownColumns(const uint16_t* img_src, int width, int channel, bool rgb3,
                                         int down_end, int column_begin, int column_end,
                                         const float* range_table_f, float inv_alpha_f)
{
    const float* src_color_hor = m_left_pass_color; // result of horizontal pass filter
    float* down_pass_color = m_down_pass_color;
    float* down_pass_factor = m_down_pass_factor;
    const int row_stride = width * channel;

    // 1st line has no previous line
    for (int k = column_begin; k < column_end; k++)
    {
        down_pass_factor[k] = 1.f;
        for (int c = 0; c < channel; c++)
        {
            down_pass_color[k * channel + c] = src_color_hor[k * channel + c];
        }
    }

    for (int row = width; row < down_end; row += width)
    {
        const int k_begin = row + column_begin;
        if (k_begin >= down_end) break;
        const int k_end = std::min(row + column_end, down_end);
        for (int k = k_begin; k < k_end; k++)
        {
            const uint16_t* src_color = img_src + k * channel;
            int diff = rgb3
                ? getDiffFactorRgb3(src_color, src_color - row_stride)
                : getDiffFactor(src_color, src_color - row_stride);

            float alpha_f = range_table_f[diff];

            down_pass_factor[k] = inv_alpha_f + alpha_f * (down_pass_factor[k - width]);

            float* color = down_pass_color + k * channel;
            const float* prev_color = color - row_stride;
            const float* hor = src_color_hor + k * channel;
            for (int c = 0; c < channel; c++)
            {
                color[c] = inv_alpha_f * (hor[c]) + alpha_f * (prev_color[c]);
            }
        }
    }
}

void CRBFilterPlain::verticalUpColumns(const uint16_t* img_src, int width, int height, int channel, bool rgb3,
                                       int up_begin, int column_begin, int column_end,
                                       const float* range_table_f, float inv_alpha_f)
{
    const float* src_color_hor = m_left_pass_color; // result of horizontal pass filter
    float* up_pass_color = m_up_pass_color;
    float* up_pass_factor = m_up_pass_factor;
    const int row_stride = width * channel;
    const int last_row = (height - 1) * width;

    // 1st line (the image's last row) has no previous line
    for (int k = last_row + column_begin; k < last_row + column_end; k++)
    {
        up_pass_factor[k] = 1.f;
        for (int c = 0; c < channel; c++)
        {
            up_pass_color[k * channel + c] = src_color_hor[k * channel + c];
        }
    }

    for (int row = last_row - width; row >= 0; row -= width)
    {
        const int k_end = row + column_end;
        if (k_end <= up_begin) break;
        const int k_begin = std::max(row + column_begin, up_begin);
        for (int k = k_begin; k < k_end; k++)
        {
            // The legacy pass compares one element before pixel k (its pointer
            // sits on the previous pixel's last channel); keep that exactly.
            const uint16_t* src_color = img_src + k * channel - 1;
            int diff = rgb3
                ? getDiffFactorRgb3(src_color, src_color + row_stride)
                : getDiffFactor(src_color, src_color + row_stride);

            float alpha_f = range_table_f[diff];

            up_pass_factor[k] = inv_alpha_f + alpha_f * (up_pass_factor[k + width]);

            float* color = up_pass_color + k * channel;
            const float* prev_color = color + row_stride;
            const float* hor = src_color_hor + k * channel;
            for (int c = 0; c < channel; c++)
            {
                const float src_term = inv_alpha_f * (hor[c]);
                const float prev_term = alpha_f * (prev_color[c]);
                color[c] = src_term + prev_term;
            }
        }
    }
}

// One row of the legacy left and right passes (same per-element code as the
// row-parallel loops in filter()). With 3 channels the horizontal average of
// the row follows at once: it only reads and writes that row's pixels.
void CRBFilterPlain::horizontalRow(const uint16_t* img_src, int y, int width, int height, int channel,
                                   bool rgb3, const float* range_table_f, float inv_alpha_f)
{
    if (y < height - 1)
    {
        const int row_color_offset = y * width * channel;
        const int row_factor_offset = y * width;
        const uint16_t* src_color = img_src + row_color_offset;
        float* left_pass_color = m_left_pass_color + row_color_offset;
        float* left_pass_factor = m_left_pass_factor + row_factor_offset;
        const uint16_t* src_prev = src_color;
        const float* prev_factor = left_pass_factor;
        const float* prev_color = left_pass_color;

        // process 1st pixel separately since it has no previous
        *left_pass_factor++ = 1.f;
        for (int c = 0; c < channel; c++)
        {
            *left_pass_color++ = *src_color++;
        }

        // handle other pixels
        for (int x = 1; x < width; x++)
        {
            int diff = rgb3
                ? getDiffFactorRgb3(src_color, src_prev)
                : getDiffFactor(src_color, src_prev);
            src_prev = src_color;

            float alpha_f = range_table_f[diff];

            *left_pass_factor++ = inv_alpha_f + alpha_f * (*prev_factor++);

            for (int c = 0; c < channel; c++)
            {
                *left_pass_color++ = inv_alpha_f * (*src_color++) + alpha_f * (*prev_color++);
            }
        }
    }

    if (y >= 1)
    {
        const int row_color_end = ((y + 1) * width * channel) - 1;
        const int row_factor_end = ((y + 1) * width) - 1;
        const uint16_t* src_color = img_src + row_color_end;
        float* right_pass_color = m_right_pass_color + row_color_end;
        float* right_pass_factor = m_right_pass_factor + row_factor_end;
        const float* prev_factor = right_pass_factor;
        const float* prev_color = right_pass_color;

        // process 1st pixel separately since it has no previous
        *right_pass_factor-- = 1.f;
        for (int c = 0; c < channel; c++)
        {
            *right_pass_color-- = *src_color--;
        }

        // handle other pixels
        for (int x = 1; x < width; x++)
        {
            // Preserve the original pointer arithmetic, including the hard-coded
            // neighbor stride used by this legacy filter.
            int diff = rgb3
                ? getDiffFactorRgb3(src_color, src_color - 3)
                : getDiffFactor(src_color, src_color - 3);

            float alpha_f = range_table_f[diff];

            *right_pass_factor-- = inv_alpha_f + alpha_f * (*prev_factor--);

            for (int c = 0; c < channel; c++)
            {
                *right_pass_color-- = inv_alpha_f * (*src_color--) + alpha_f * (*prev_color--);
            }
        }
    }

    if (rgb3)
    {
        float* img_out = m_left_pass_color; // use as temporary buffer
        const float* left_pass_color = m_left_pass_color;
        const float* left_pass_factor = m_left_pass_factor;
        const float* right_pass_color = m_right_pass_color;
        const float* right_pass_factor = m_right_pass_factor;
        for (int i = y * width; i < (y + 1) * width; i++)
        {
            // average color divided by average factor
            float factor = 1.f / ((left_pass_factor[i]) + (right_pass_factor[i]));
            for (int c = 0; c < channel; c++)
            {
                int idx = i + i + i + c;
                img_out[idx] = (factor * ((left_pass_color[idx]) + (right_pass_color[idx])));
            }
        }
    }
}

// The legacy output loops of filter() over the pixel range [begin, end).
void CRBFilterPlain::outputRange(uint16_t* img_dst, int begin, int end, int channel,
                                 const uint16_t* output_lut, bool output_curve_index,
                                 const int32_t* output_curve_r,
                                 const int32_t* output_curve_g,
                                 const int32_t* output_curve_b) const
{
    const float* down_pass_color = m_down_pass_color;
    const float* down_pass_factor = m_down_pass_factor;
    const float* up_pass_color = m_up_pass_color;
    const float* up_pass_factor = m_up_pass_factor;

    if (output_lut)
    {
        if (channel == 3)
        {
            if (output_curve_index)
            {
                for (int i = begin; i < end; i++)
                {
                    // average color divided by average factor
                    float factor = 1.f / ((up_pass_factor[i]) + (down_pass_factor[i]));
                    const float* up_pass_color_ptr = up_pass_color + (i * 3);
                    const float* down_pass_color_ptr = down_pass_color + (i * 3);
                    uint16_t* img_dst_ptr = img_dst + (i * 3);
                    const uint16_t filtered_r =
                        output_lut[(uint16_t)(factor * ((up_pass_color_ptr[0]) + (down_pass_color_ptr[0])))];
                    const uint16_t filtered_g =
                        output_lut[(uint16_t)(factor * ((up_pass_color_ptr[1]) + (down_pass_color_ptr[1])))];
                    const uint16_t filtered_b =
                        output_lut[(uint16_t)(factor * ((up_pass_color_ptr[2]) + (down_pass_color_ptr[2])))];
                    const int32_t curve_index =
                        ((output_curve_r[filtered_r] << 2)
                       + (output_curve_g[filtered_g] * 11)
                       +  output_curve_b[filtered_b]) >> 4;
                    const uint16_t mask_value = limitU16FromInt32(curve_index);
                    img_dst_ptr[0] = mask_value;
                    img_dst_ptr[1] = mask_value;
                    img_dst_ptr[2] = mask_value;
                }
            }
            else
            {
                for (int i = begin; i < end; i++)
                {
                    // average color divided by average factor
                    float factor = 1.f / ((up_pass_factor[i]) + (down_pass_factor[i]));
                    const float* up_pass_color_ptr = up_pass_color + (i * 3);
                    const float* down_pass_color_ptr = down_pass_color + (i * 3);
                    uint16_t* img_dst_ptr = img_dst + (i * 3);
                    img_dst_ptr[0] =
                        output_lut[(uint16_t)(factor * ((up_pass_color_ptr[0]) + (down_pass_color_ptr[0])))];
                    img_dst_ptr[1] =
                        output_lut[(uint16_t)(factor * ((up_pass_color_ptr[1]) + (down_pass_color_ptr[1])))];
                    img_dst_ptr[2] =
                        output_lut[(uint16_t)(factor * ((up_pass_color_ptr[2]) + (down_pass_color_ptr[2])))];
                }
            }
        }
        else
        {
            for (int i = begin; i < end; i++)
            {
                // average color divided by average factor
                float factor = 1.f / ((up_pass_factor[i]) + (down_pass_factor[i]));
                for (int c = 0; c < channel; c++)
                {
                    int idx = i + i + i + c;
                    const uint16_t filtered =
                        (uint16_t)(factor * ((up_pass_color[idx]) + (down_pass_color[idx])));
                    img_dst[idx] = output_lut[filtered];
                }
            }
        }
    }
    else
    {
        if (channel == 3)
        {
            for (int i = begin; i < end; i++)
            {
                // average color divided by average factor
                float factor = 1.f / ((up_pass_factor[i]) + (down_pass_factor[i]));
                int idx = i + i + i;
                img_dst[idx] =
                    (uint16_t)(factor * ((up_pass_color[idx]) + (down_pass_color[idx])));
                img_dst[idx + 1] =
                    (uint16_t)(factor * ((up_pass_color[idx + 1]) + (down_pass_color[idx + 1])));
                img_dst[idx + 2] =
                    (uint16_t)(factor * ((up_pass_color[idx + 2]) + (down_pass_color[idx + 2])));
            }
        }
        else
        {
            for (int i = begin; i < end; i++)
            {
                // average color divided by average factor
                float factor = 1.f / ((up_pass_factor[i]) + (down_pass_factor[i]));
                for (int c = 0; c < channel; c++)
                {
                    int idx = i + i + i + c;
                    img_dst[idx] =
                        (uint16_t)(factor * ((up_pass_color[idx]) + (down_pass_color[idx])));
                }
            }
        }
    }
}

// Default path of filter(): the same passes in one parallel region with a
// single barrier.
//  1. Rows in parallel: left pass, right pass and (3 channels) the horizontal
//     average of each row.
//  2. Column blocks in parallel: the down pass, the up pass and then the output
//     of the block's pixels. Pixel i only needs down[i] and up[i], which lie on
//     the block's own chains, so no barrier is needed between the three.
// Timing: left_ms is phase 1, vertical_down_ms is phase 2 (down, up and output
// together); the other per-pass fields stay 0 on this path.
void CRBFilterPlain::filterColumnParallel(const uint16_t* img_src, uint16_t* img_dst,
                                          int width, int height, int channel, bool rgb3,
                                          int down_end, int up_begin,
                                          const float* range_table_f, float inv_alpha_f,
                                          const uint16_t* output_lut, bool output_curve_index,
                                          const int32_t* output_curve_r,
                                          const int32_t* output_curve_g,
                                          const int32_t* output_curve_b)
{
    const bool timing_enabled = m_timing_enabled;
    const int width_height = width * height;
    // Column blocks of a multiple of 16 pixels, about two per thread, so a
    // busy core does not hold the whole phase back.
    int threads = std::max(1, omp_get_max_threads());
    if (m_max_threads > 0) threads = std::min(threads, m_max_threads);
    const int block_width =
        std::max(16, ((width + threads * 2 - 1) / (threads * 2) + 15) & ~15);
    const int blocks = (width + block_width - 1) / block_width;
    const double phase_start = timing_enabled ? omp_get_wtime() : 0.0;
    double horizontal_end = 0.0;

    #pragma omp parallel num_threads(threads)
    {
        #pragma omp for schedule(static)
        for (int y = 0; y < height; y++)
        {
            horizontalRow(img_src, y, width, height, channel, rgb3, range_table_f, inv_alpha_f);
        }

        if (!rgb3)
        {
            // Legacy horizontal average; with 4 channels its i*3+c index spans
            // neighbouring pixels, so it cannot be fused into the rows.
            float* img_out = m_left_pass_color; // use as temporary buffer
            const float* left_pass_color = m_left_pass_color;
            const float* left_pass_factor = m_left_pass_factor;
            const float* right_pass_color = m_right_pass_color;
            const float* right_pass_factor = m_right_pass_factor;

            #pragma omp for
            for (int i = 0; i < width_height; i++)
            {
                // average color divided by average factor
                float factor = 1.f / ((left_pass_factor[i]) + (right_pass_factor[i]));
                for (int c = 0; c < channel; c++)
                {
                    int idx = i + i + i + c;
                    img_out[idx] = (factor * ((left_pass_color[idx]) + (right_pass_color[idx])));
                }
            }
        }

        if( timing_enabled )
        {
            #pragma omp master
            {
                horizontal_end = omp_get_wtime();
            }
        }

        #pragma omp for schedule(dynamic, 1)
        for (int b = 0; b < blocks; b++)
        {
            const int column_begin = b * block_width;
            const int column_end = std::min(width, column_begin + block_width);
            verticalDownColumns(img_src, width, channel, rgb3, down_end,
                                column_begin, column_end, range_table_f, inv_alpha_f);
            verticalUpColumns(img_src, width, height, channel, rgb3, up_begin,
                              column_begin, column_end, range_table_f, inv_alpha_f);
            if (rgb3)
            {
                for (int row = 0; row < width_height; row += width)
                {
                    outputRange(img_dst, row + column_begin, row + column_end, channel,
                                output_lut, output_curve_index,
                                output_curve_r, output_curve_g, output_curve_b);
                }
            }
        }

        if (!rgb3)
        {
            // Legacy output order: with 4 channels the i*3+c index spans
            // neighbouring pixels, so later pixels must overwrite earlier ones.
            #pragma omp for
            for (int i = 0; i < width_height; i++)
            {
                outputRange(img_dst, i, i + 1, channel,
                            output_lut, output_curve_index,
                            output_curve_r, output_curve_g, output_curve_b);
            }
        }
    }

    if( timing_enabled )
    {
        const double end = omp_get_wtime();
        m_last_timing.left_ms = (horizontal_end - phase_start) * 1000.0;
        m_last_timing.vertical_down_ms = (end - horizontal_end) * 1000.0;
    }
}

// memory must be reserved before calling image filter
// this implementation of filter uses plain C++, single threaded
// channel count must be 3 or 4 (alpha not used)
void CRBFilterPlain::filter(uint16_t* __restrict img_src, uint16_t* __restrict img_dst,
	float sigma_spatial, float sigma_range,
	int width, int height, int channel,
    const uint16_t * __restrict output_lut,
    const int32_t * __restrict output_curve_r,
    const int32_t * __restrict output_curve_g,
    const int32_t * __restrict output_curve_b)
{
    const bool timing_enabled = m_timing_enabled;
    const double filter_start = timing_enabled ? omp_get_wtime() : 0.0;
    m_last_timing = RBFilterPlainTiming();

    if(!img_src) return;
    if(!img_dst) return;
    if(!(m_reserve_channels == channel)) return;
    if(!(m_reserve_width >= width)) return;
    if(!(m_reserve_height >= height)) return;

    const int width_height = width * height;
    const int width_height_channel = width_height * channel;
    const bool rgb3 = (channel == 3);
    const bool output_curve_index =
        output_lut
        && output_curve_r
        && output_curve_g
        && output_curve_b
        && channel == 3;

    const int last_row_color_offset = (height - 1) * width * channel;
    const int down_written_pixels = width + (height - 1) * (width - 1);
    const int up_unwritten_pixels = height - 1;

    const double boundary_start = timing_enabled ? omp_get_wtime() : 0.0;
    memset(m_left_pass_color + last_row_color_offset,
           0,
           (size_t)(width_height_channel - last_row_color_offset) * sizeof(float));
    memset(m_left_pass_factor + (height - 1) * width,
           0,
           (size_t)(width_height - (height - 1) * width) * sizeof(float));

    memset(m_right_pass_color,
           0,
           (size_t)width * (size_t)channel * sizeof(float));
    memset(m_right_pass_factor,
           0,
           (size_t)width * sizeof(float));

    memset(m_down_pass_color + down_written_pixels * channel,
           0,
           (size_t)(width_height_channel - down_written_pixels * channel) * sizeof(float));
    memset(m_down_pass_factor + down_written_pixels,
           0,
           (size_t)(width_height - down_written_pixels) * sizeof(float));

    memset(m_up_pass_color,
           0,
           (size_t)up_unwritten_pixels * (size_t)channel * sizeof(float));
    memset(m_up_pass_factor,
           0,
           (size_t)up_unwritten_pixels * sizeof(float));
    if( timing_enabled )
    {
        m_last_timing.boundary_ms = (omp_get_wtime() - boundary_start) * 1000.0;
    }

    // compute a lookup table
    const double range_table_start = timing_enabled ? omp_get_wtime() : 0.0;
    float alpha_f = static_cast<float>(exp(-sqrt(2.0) / (sigma_spatial * QX_DEF_U16_MAX)));
    float inv_alpha_f = 1.f - alpha_f;
    float inv_sigma_range = 1.0f / (sigma_range * QX_DEF_U16_MAX);

    if (!m_range_table)
    {
        m_range_table = new float[QX_DEF_U16_MAX + 1];
        m_range_table_valid = false;
    }
    if (!m_range_table_valid
        || m_range_table_alpha != alpha_f
        || m_range_table_inv_sigma != inv_sigma_range)
    {
        float * range_table_f = m_range_table;
#pragma omp parallel for
        for (int i = 0; i <= QX_DEF_U16_MAX; i++)
        {
            const float ii = static_cast<float>(-i);
            range_table_f[i] = alpha_f * exp(ii * inv_sigma_range);
        }
        m_range_table_alpha = alpha_f;
        m_range_table_inv_sigma = inv_sigma_range;
        m_range_table_valid = true;
    }
    const float * range_table_f = m_range_table;
    if( timing_enabled )
    {
        m_last_timing.range_table_ms = (omp_get_wtime() - range_table_start) * 1000.0;
    }

    if (m_parallel_vertical)
    {
        filterColumnParallel(img_src, img_dst, width, height, channel, rgb3,
                             down_written_pixels, up_unwritten_pixels,
                             range_table_f, inv_alpha_f,
                             output_lut, output_curve_index,
                             output_curve_r, output_curve_g, output_curve_b);
        if( timing_enabled )
        {
            m_last_timing.total_ms = (omp_get_wtime() - filter_start) * 1000.0;
        }
        return;
    }

    double left_start = 0.0;
    double right_start = 0.0;
    double horizontal_average_start = 0.0;
    double down_ms = 0.0;
    double up_ms = 0.0;
    double output_start = 0.0;

    #pragma omp parallel
    {
        if( timing_enabled )
        {
            #pragma omp master
            {
                left_start = omp_get_wtime();
            }
            #pragma omp barrier
        }

        // Horizontal rows are independent; process the left and right scans row-parallel.
        #pragma omp for nowait
        for (int y = 0; y < height-1; y++)
        {
            const int row_color_offset = y * width * channel;
            const int row_factor_offset = y * width;
            const uint16_t* src_color = img_src + row_color_offset;
            float* left_pass_color = m_left_pass_color + row_color_offset;
            float* left_pass_factor = m_left_pass_factor + row_factor_offset;
            const uint16_t* src_prev = src_color;
            const float* prev_factor = left_pass_factor;
            const float* prev_color = left_pass_color;

            // process 1st pixel separately since it has no previous
            *left_pass_factor++ = 1.f;
            for (int c = 0; c < channel; c++)
            {
                *left_pass_color++ = *src_color++;
            }

            // handle other pixels
            for (int x = 1; x < width; x++)
            {
                // determine difference in pixel color between current and previous
                // calculation is different depending on number of channels
                int diff = rgb3
                    ? getDiffFactorRgb3(src_color, src_prev)
                    : getDiffFactor(src_color, src_prev);
                src_prev = src_color;

                float alpha_f = range_table_f[diff];

                *left_pass_factor++ = inv_alpha_f + alpha_f * (*prev_factor++);

                for (int c = 0; c < channel; c++)
                {
                    *left_pass_color++ = inv_alpha_f * (*src_color++) + alpha_f * (*prev_color++);
                }
            }
        }

        if( timing_enabled )
        {
            #pragma omp barrier
            #pragma omp master
            {
                m_last_timing.left_ms = (omp_get_wtime() - left_start) * 1000.0;
                right_start = omp_get_wtime();
            }
            #pragma omp barrier
        }

        #pragma omp for
        for (int reverse_y = 0; reverse_y < height-1; reverse_y++)
        {
            const int y = height - 1 - reverse_y;
            const int row_color_end = ((y + 1) * width * channel) - 1;
            const int row_factor_end = ((y + 1) * width) - 1;
            const uint16_t* src_color = img_src + row_color_end;
            float* right_pass_color = m_right_pass_color + row_color_end;
            float* right_pass_factor = m_right_pass_factor + row_factor_end;
            const float* prev_factor = right_pass_factor;
            const float* prev_color = right_pass_color;

            // process 1st pixel separately since it has no previous
            *right_pass_factor-- = 1.f;
            for (int c = 0; c < channel; c++)
            {
                *right_pass_color-- = *src_color--;
            }

            // handle other pixels
            for (int x = 1; x < width; x++)
            {
                // Preserve the original pointer arithmetic, including the hard-coded
                // neighbor stride used by this legacy filter.
                int diff = rgb3
                    ? getDiffFactorRgb3(src_color, src_color - 3)
                    : getDiffFactor(src_color, src_color - 3);

                float alpha_f = range_table_f[diff];

                *right_pass_factor-- = inv_alpha_f + alpha_f * (*prev_factor--);

                for (int c = 0; c < channel; c++)
                {
                    *right_pass_color-- = inv_alpha_f * (*src_color--) + alpha_f * (*prev_color--);
                }
            }
        }

        if( timing_enabled )
        {
            #pragma omp master
            {
                m_last_timing.right_ms = (omp_get_wtime() - right_start) * 1000.0;
                horizontal_average_start = omp_get_wtime();
            }
            #pragma omp barrier
        }

        // vertical pass will be applied on top on horizontal pass, while using pixel differences from original image
        // result color stored in 'm_left_pass_color' and vertical pass will use it as source color
        {
            float* img_out = m_left_pass_color; // use as temporary buffer
            const float* left_pass_color = m_left_pass_color;
            const float* left_pass_factor = m_left_pass_factor;
            const float* right_pass_color = m_right_pass_color;
            const float* right_pass_factor = m_right_pass_factor;

            #pragma omp for
            for (int i = 0; i < width_height; i++)
            {
                // average color divided by average factor
                float factor = 1.f / ((left_pass_factor[i]) + (right_pass_factor[i]));
                for (int c = 0; c < channel; c++)
                {
                    int idx = i + i + i + c;
                    img_out[idx] = (factor * ((left_pass_color[idx]) + (right_pass_color[idx])));
                }
            }
        }

        if( timing_enabled )
        {
            #pragma omp master
            {
                m_last_timing.horizontal_average_ms =
                    (omp_get_wtime() - horizontal_average_start) * 1000.0;
            }
            #pragma omp barrier
        }

    }

    auto runVerticalDown = [&]()
    {
        const double vertical_pass_start =
            timing_enabled ? omp_get_wtime() : 0.0;
        const float* src_color_hor = m_left_pass_color; // result of horizontal pass filter

        const uint16_t* src_color = img_src;
        float* down_pass_color = m_down_pass_color;
        float* down_pass_factor = m_down_pass_factor;

        const uint16_t* src_prev = src_color;
        const float* prev_color = down_pass_color;
        const float* prev_factor = down_pass_factor;

        // 1st line done separately because no previous line
        for (int x = 0; x < width; x++)
        {
            *down_pass_factor++ = 1.f;
            for (int c = 0; c < channel; c++)
            {
                *down_pass_color++ = *src_color_hor++;
            }
            src_color += channel;
        }

        // handle other lines
        for (int y = 1; y < height; y++)
        {
            for (int x = 0; x < width-1; x++)
            {
                // determine difference in pixel color between current and previous
                // calculation is different depending on number of channels
                int diff = rgb3
                    ? getDiffFactorRgb3(src_color, src_prev)
                    : getDiffFactor(src_color, src_prev);
                src_prev += channel;
                src_color += channel;

                float alpha_f = range_table_f[diff];

                *down_pass_factor++ = inv_alpha_f + alpha_f * (*prev_factor++);

                for (int c = 0; c < channel; c++)
                {
                    *down_pass_color++ = inv_alpha_f * (*src_color_hor++) + alpha_f * (*prev_color++);
                }
            }
        }
        if( timing_enabled )
        {
            down_ms = (omp_get_wtime() - vertical_pass_start) * 1000.0;
        }
    };

    auto runVerticalUp = [&]()
    {
        const double vertical_pass_start =
            timing_enabled ? omp_get_wtime() : 0.0;
        const double vertical_up_first_line_start =
            timing_enabled ? omp_get_wtime() : 0.0;
        // start from end and then go up to begining
        int last_index = width * height * channel - 1;
        const uint16_t* src_color = img_src + last_index;
        const float* src_color_hor = m_left_pass_color + last_index; // result of horizontal pass filter
        float* up_pass_color = m_up_pass_color + last_index;
        float* up_pass_factor = m_up_pass_factor + (width * height - 1);

	//	const uint16_t* src_prev = src_color;
        const float* prev_color = up_pass_color;
        const float* prev_factor = up_pass_factor;

        // 1st line done separately because no previous line
        for (int x = 0; x < width; x++)
        {
            *up_pass_factor-- = 1.f;
            for (int c = 0; c < channel; c++)
            {
                *up_pass_color-- = *src_color_hor--;
            }
            src_color -= channel;
        }
        const int row_stride = width * channel;
        const uint16_t* src_color_prev = src_color + row_stride;
        double vertical_up_first_line_ms = 0.0;
        if( timing_enabled )
        {
            vertical_up_first_line_ms = (omp_get_wtime() - vertical_up_first_line_start) * 1000.0;
            m_last_timing.vertical_up_first_line_ms = vertical_up_first_line_ms;
        }

        const double vertical_up_body_start =
            timing_enabled ? omp_get_wtime() : 0.0;
        double vertical_up_body_diff_ms = 0.0;
        double vertical_up_body_store_ms = 0.0;
        double vertical_up_body_store_factor_ms = 0.0;
        double vertical_up_body_store_color_ms = 0.0;
        double vertical_up_body_store_color_src_ms = 0.0;
        double vertical_up_body_store_color_prev_ms = 0.0;
        double vertical_up_body_store_color_assign_ms = 0.0;

        if( !timing_enabled )
        {
            if( rgb3 )
            {
                for (int y = 1; y < height; y++)
                {
                    for (int x = 0; x < width-1; x++)
                    {
                        src_color -= channel;
                        src_color_prev -= channel;
                        int diff = getDiffFactorRgb3(src_color, src_color_prev);

                        float alpha_f = range_table_f[diff];

                        *up_pass_factor-- = inv_alpha_f + alpha_f * (*prev_factor--);

                        const float src_term_0 = inv_alpha_f * (*src_color_hor--);
                        const float prev_term_0 = alpha_f * (*prev_color--);
                        *up_pass_color-- = src_term_0 + prev_term_0;

                        const float src_term_1 = inv_alpha_f * (*src_color_hor--);
                        const float prev_term_1 = alpha_f * (*prev_color--);
                        *up_pass_color-- = src_term_1 + prev_term_1;

                        const float src_term_2 = inv_alpha_f * (*src_color_hor--);
                        const float prev_term_2 = alpha_f * (*prev_color--);
                        *up_pass_color-- = src_term_2 + prev_term_2;
                    }
                }
            }
            else
            {
                for (int y = 1; y < height; y++)
                {
                    for (int x = 0; x < width-1; x++)
                    {
                        src_color -= channel;
                        src_color_prev -= channel;
                        int diff = getDiffFactor(src_color, src_color_prev);

                        float alpha_f = range_table_f[diff];

                        *up_pass_factor-- = inv_alpha_f + alpha_f * (*prev_factor--);

                        for (int c = 0; c < channel; c++)
                        {
                            const float src_term = inv_alpha_f * (*src_color_hor--);
                            const float prev_term = alpha_f * (*prev_color--);
                            *up_pass_color-- = src_term + prev_term;
                        }
                    }
                }
            }
            return;
        }

        // handle other lines
        if( rgb3 )
        {
            for (int y = 1; y < height; y++)
            {
                for (int x = 0; x < width-1; x++)
                {
                    const double vertical_up_body_diff_start =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    // determine difference in pixel color between current and previous
                    src_color -= channel;
                    src_color_prev -= channel;
                    int diff = getDiffFactorRgb3(src_color, src_color_prev);

                    float alpha_f = range_table_f[diff];
                    if( timing_enabled )
                    {
                        vertical_up_body_diff_ms +=
                            (omp_get_wtime() - vertical_up_body_diff_start) * 1000.0;
                    }

                    const double vertical_up_body_store_start =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    *up_pass_factor-- = inv_alpha_f + alpha_f * (*prev_factor--);
                    if( timing_enabled )
                    {
                        vertical_up_body_store_factor_ms +=
                            (omp_get_wtime() - vertical_up_body_store_start) * 1000.0;
                    }

                    const double vertical_up_body_store_color_start =
                        timing_enabled ? omp_get_wtime() : 0.0;

                    const double vertical_up_body_store_color_src_start_0 =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    const float src_term_0 = inv_alpha_f * (*src_color_hor--);
                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_src_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_src_start_0) * 1000.0;
                    }

                    const double vertical_up_body_store_color_prev_start_0 =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    const float prev_term_0 = alpha_f * (*prev_color--);
                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_prev_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_prev_start_0) * 1000.0;
                    }

                    const double vertical_up_body_store_color_assign_start_0 =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    *up_pass_color-- = src_term_0 + prev_term_0;
                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_assign_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_assign_start_0) * 1000.0;
                    }

                    const double vertical_up_body_store_color_src_start_1 =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    const float src_term_1 = inv_alpha_f * (*src_color_hor--);
                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_src_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_src_start_1) * 1000.0;
                    }

                    const double vertical_up_body_store_color_prev_start_1 =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    const float prev_term_1 = alpha_f * (*prev_color--);
                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_prev_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_prev_start_1) * 1000.0;
                    }

                    const double vertical_up_body_store_color_assign_start_1 =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    *up_pass_color-- = src_term_1 + prev_term_1;
                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_assign_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_assign_start_1) * 1000.0;
                    }

                    const double vertical_up_body_store_color_src_start_2 =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    const float src_term_2 = inv_alpha_f * (*src_color_hor--);
                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_src_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_src_start_2) * 1000.0;
                    }

                    const double vertical_up_body_store_color_prev_start_2 =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    const float prev_term_2 = alpha_f * (*prev_color--);
                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_prev_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_prev_start_2) * 1000.0;
                    }

                    const double vertical_up_body_store_color_assign_start_2 =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    *up_pass_color-- = src_term_2 + prev_term_2;
                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_assign_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_assign_start_2) * 1000.0;
                    }

                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_start) * 1000.0;
                        vertical_up_body_store_ms +=
                            (omp_get_wtime() - vertical_up_body_store_start) * 1000.0;
                    }
                }
            }
        }
        else
        {
            for (int y = 1; y < height; y++)
            {
                for (int x = 0; x < width-1; x++)
                {
                    const double vertical_up_body_diff_start =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    // determine difference in pixel color between current and previous
                    // calculation is different depending on number of channels
                    src_color -= channel;
                    src_color_prev -= channel;
                    int diff = getDiffFactor(src_color, src_color_prev);

                    float alpha_f = range_table_f[diff];
                    if( timing_enabled )
                    {
                        vertical_up_body_diff_ms +=
                            (omp_get_wtime() - vertical_up_body_diff_start) * 1000.0;
                    }

                    const double vertical_up_body_store_start =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    *up_pass_factor-- = inv_alpha_f + alpha_f * (*prev_factor--);
                    if( timing_enabled )
                    {
                        vertical_up_body_store_factor_ms +=
                            (omp_get_wtime() - vertical_up_body_store_start) * 1000.0;
                    }

                    const double vertical_up_body_store_color_start =
                        timing_enabled ? omp_get_wtime() : 0.0;
                    for (int c = 0; c < channel; c++)
                    {
                        const double vertical_up_body_store_color_src_start =
                            timing_enabled ? omp_get_wtime() : 0.0;
                        const float src_term = inv_alpha_f * (*src_color_hor--);
                        if( timing_enabled )
                        {
                            vertical_up_body_store_color_src_ms +=
                                (omp_get_wtime() - vertical_up_body_store_color_src_start) * 1000.0;
                        }

                        const double vertical_up_body_store_color_prev_start =
                            timing_enabled ? omp_get_wtime() : 0.0;
                        const float prev_term = alpha_f * (*prev_color--);
                        if( timing_enabled )
                        {
                            vertical_up_body_store_color_prev_ms +=
                                (omp_get_wtime() - vertical_up_body_store_color_prev_start) * 1000.0;
                        }

                        const double vertical_up_body_store_color_assign_start =
                            timing_enabled ? omp_get_wtime() : 0.0;
                        *up_pass_color-- = src_term + prev_term;
                        if( timing_enabled )
                        {
                            vertical_up_body_store_color_assign_ms +=
                                (omp_get_wtime() - vertical_up_body_store_color_assign_start) * 1000.0;
                        }
                    }
                    if( timing_enabled )
                    {
                        vertical_up_body_store_color_ms +=
                            (omp_get_wtime() - vertical_up_body_store_color_start) * 1000.0;
                        vertical_up_body_store_ms +=
                            (omp_get_wtime() - vertical_up_body_store_start) * 1000.0;
                    }
                }
            }
        }
        if( timing_enabled )
        {
            m_last_timing.vertical_up_body_diff_ms = vertical_up_body_diff_ms;
            m_last_timing.vertical_up_body_store_ms = vertical_up_body_store_ms;
            m_last_timing.vertical_up_body_store_factor_ms = vertical_up_body_store_factor_ms;
            m_last_timing.vertical_up_body_store_color_ms = vertical_up_body_store_color_ms;
            m_last_timing.vertical_up_body_store_color_src_ms = vertical_up_body_store_color_src_ms;
            m_last_timing.vertical_up_body_store_color_prev_ms = vertical_up_body_store_color_prev_ms;
            m_last_timing.vertical_up_body_store_color_assign_ms = vertical_up_body_store_color_assign_ms;
            m_last_timing.vertical_up_body_ms =
                (omp_get_wtime() - vertical_up_body_start) * 1000.0;
            up_ms = (omp_get_wtime() - vertical_pass_start) * 1000.0;
        }
    };

    #pragma omp parallel sections num_threads(2)
    {
        #pragma omp section
        {
            runVerticalDown();
        }
        #pragma omp section
        {
            runVerticalUp();
        }
    }

    if( timing_enabled )
    {
        m_last_timing.vertical_down_ms = down_ms;
        m_last_timing.vertical_up_ms = up_ms;
        output_start = omp_get_wtime();
    }

    #pragma omp parallel
    {

        ///////////////
        // average result of vertical pass is written to output buffer
        {
            const float* down_pass_color = m_down_pass_color;
            const float* down_pass_factor = m_down_pass_factor;
            const float* up_pass_color = m_up_pass_color;
            const float* up_pass_factor = m_up_pass_factor;

            if (output_lut)
            {
                if (channel == 3)
                {
                    if (output_curve_index)
                    {
                        #pragma omp for
                        for (int i = 0; i < width_height; i++)
                        {
                            // average color divided by average factor
                            float factor = 1.f / ((up_pass_factor[i]) + (down_pass_factor[i]));
                            const float* up_pass_color_ptr = up_pass_color + (i * 3);
                            const float* down_pass_color_ptr = down_pass_color + (i * 3);
                            uint16_t* img_dst_ptr = img_dst + (i * 3);
                            const uint16_t filtered_r =
                                output_lut[(uint16_t)(factor * ((up_pass_color_ptr[0]) + (down_pass_color_ptr[0])))];
                            const uint16_t filtered_g =
                                output_lut[(uint16_t)(factor * ((up_pass_color_ptr[1]) + (down_pass_color_ptr[1])))];
                            const uint16_t filtered_b =
                                output_lut[(uint16_t)(factor * ((up_pass_color_ptr[2]) + (down_pass_color_ptr[2])))];
                            const int32_t curve_index =
                                ((output_curve_r[filtered_r] << 2)
                               + (output_curve_g[filtered_g] * 11)
                               +  output_curve_b[filtered_b]) >> 4;
                            const uint16_t mask_value = limitU16FromInt32(curve_index);
                            img_dst_ptr[0] = mask_value;
                            img_dst_ptr[1] = mask_value;
                            img_dst_ptr[2] = mask_value;
                        }
                    }
                    else
                    {
                        #pragma omp for
                        for (int i = 0; i < width_height; i++)
                        {
                            // average color divided by average factor
                            float factor = 1.f / ((up_pass_factor[i]) + (down_pass_factor[i]));
                            const float* up_pass_color_ptr = up_pass_color + (i * 3);
                            const float* down_pass_color_ptr = down_pass_color + (i * 3);
                            uint16_t* img_dst_ptr = img_dst + (i * 3);
                            img_dst_ptr[0] =
                                output_lut[(uint16_t)(factor * ((up_pass_color_ptr[0]) + (down_pass_color_ptr[0])))];
                            img_dst_ptr[1] =
                                output_lut[(uint16_t)(factor * ((up_pass_color_ptr[1]) + (down_pass_color_ptr[1])))];
                            img_dst_ptr[2] =
                                output_lut[(uint16_t)(factor * ((up_pass_color_ptr[2]) + (down_pass_color_ptr[2])))];
                        }
                    }
                }
                else
                {
                    #pragma omp for
                    for (int i = 0; i < width_height; i++)
                    {
                        // average color divided by average factor
                        float factor = 1.f / ((up_pass_factor[i]) + (down_pass_factor[i]));
                        for (int c = 0; c < channel; c++)
                        {
                            int idx = i + i + i + c;
                            const uint16_t filtered =
                                (uint16_t)(factor * ((up_pass_color[idx]) + (down_pass_color[idx])));
                            img_dst[idx] = output_lut[filtered];
                        }
                    }
                }
            }
            else
            {
                if (channel == 3)
                {
                    #pragma omp for
                    for (int i = 0; i < width_height; i++)
                    {
                        // average color divided by average factor
                        float factor = 1.f / ((up_pass_factor[i]) + (down_pass_factor[i]));
                        int idx = i + i + i;
                        img_dst[idx] =
                            (uint16_t)(factor * ((up_pass_color[idx]) + (down_pass_color[idx])));
                        img_dst[idx + 1] =
                            (uint16_t)(factor * ((up_pass_color[idx + 1]) + (down_pass_color[idx + 1])));
                        img_dst[idx + 2] =
                            (uint16_t)(factor * ((up_pass_color[idx + 2]) + (down_pass_color[idx + 2])));
                    }
                }
                else
                {
                    #pragma omp for
                    for (int i = 0; i < width_height; i++)
                    {
                        // average color divided by average factor
                        float factor = 1.f / ((up_pass_factor[i]) + (down_pass_factor[i]));
                        for (int c = 0; c < channel; c++)
                        {
                            int idx = i + i + i + c;
                            img_dst[idx] =
                                (uint16_t)(factor * ((up_pass_color[idx]) + (down_pass_color[idx])));
                        }
                    }
                }
            }
        }

        if( timing_enabled )
        {
            #pragma omp master
            {
                m_last_timing.output_ms = (omp_get_wtime() - output_start) * 1000.0;
            }
        }
    }
    if( timing_enabled )
    {
        m_last_timing.total_ms = (omp_get_wtime() - filter_start) * 1000.0;
    }
}
