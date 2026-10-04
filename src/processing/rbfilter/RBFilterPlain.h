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
*/

#pragma once

#include "stdint.h"

struct RBFilterPlainTiming
{
    double total_ms = 0.0;
    double boundary_ms = 0.0;
    double range_table_ms = 0.0;
    double left_ms = 0.0;
    double right_ms = 0.0;
    double horizontal_average_ms = 0.0;
    double vertical_down_ms = 0.0;
    double vertical_up_first_line_ms = 0.0;
    double vertical_up_body_ms = 0.0;
    double vertical_up_body_diff_ms = 0.0;
    double vertical_up_body_store_ms = 0.0;
    double vertical_up_body_store_factor_ms = 0.0;
    double vertical_up_body_store_color_ms = 0.0;
    double vertical_up_body_store_color_src_ms = 0.0;
    double vertical_up_body_store_color_prev_ms = 0.0;
    double vertical_up_body_store_color_assign_ms = 0.0;
    double vertical_up_ms = 0.0;
    double output_ms = 0.0;
};

// This class is useful only for the sake of understanding the main principles of Recursive Bilateral Filter
// It is designed in non-optimal but easy to understand way. It also does not match 1:1 with original, 
// some creative liberties were taken with original idea.
// This class is not used in performance tests

class CRBFilterPlain
{
	int			m_reserve_width = 0;
	int			m_reserve_height = 0;
	int			m_reserve_channels = 0;

	float*		m_left_pass_color = nullptr;
	float*		m_left_pass_factor = nullptr;

	float*		m_right_pass_color = nullptr;
	float*		m_right_pass_factor = nullptr;

	float*		m_down_pass_color = nullptr;
	float*		m_down_pass_factor = nullptr;

	float*		m_up_pass_color = nullptr;
	float*		m_up_pass_factor = nullptr;
	float*		m_range_table = nullptr;
	float		m_range_table_alpha = 0.0f;
	float		m_range_table_inv_sigma = 0.0f;
	bool		m_range_table_valid = false;
    bool        m_timing_enabled = false;
    bool        m_parallel_vertical = true;
    int         m_max_threads = 0;
    RBFilterPlainTiming m_last_timing;

    int getDiffFactor(const uint16_t* color1, const uint16_t* color2) const;
    void verticalDownColumns(const uint16_t* img_src, int width, int channel, bool rgb3,
                             int down_end, int column_begin, int column_end,
                             const float* range_table_f, float inv_alpha_f);
    void verticalUpColumns(const uint16_t* img_src, int width, int height, int channel, bool rgb3,
                           int up_begin, int column_begin, int column_end,
                           const float* range_table_f, float inv_alpha_f);
    void horizontalRow(const uint16_t* img_src, int y, int width, int height, int channel,
                       bool rgb3, const float* range_table_f, float inv_alpha_f);
    void outputRange(uint16_t* img_dst, int begin, int end, int channel,
                     const uint16_t* output_lut, bool output_curve_index,
                     const int32_t* output_curve_r,
                     const int32_t* output_curve_g,
                     const int32_t* output_curve_b) const;
    void filterColumnParallel(const uint16_t* img_src, uint16_t* img_dst,
                              int width, int height, int channel, bool rgb3,
                              int down_end, int up_begin,
                              const float* range_table_f, float inv_alpha_f,
                              const uint16_t* output_lut, bool output_curve_index,
                              const int32_t* output_curve_r,
                              const int32_t* output_curve_g,
                              const int32_t* output_curve_b);

public:

	CRBFilterPlain();
	~CRBFilterPlain();

	// assumes 3/4 channel images, 1 byte per channel
	void reserveMemory(int max_width, int max_height, int channels);
	void releaseMemory();

	// memory must be reserved before calling image filter
	// this implementation of filter uses plain C++, single threaded
	// channel count must be 3 or 4 (alpha not used)
    void filter(uint16_t* __restrict img_src, uint16_t* __restrict img_dst,
		float sigma_spatial, float sigma_range,
		int width, int height, int channel,
        const uint16_t * __restrict output_lut = nullptr,
        const int32_t * __restrict output_curve_r = nullptr,
        const int32_t * __restrict output_curve_g = nullptr,
        const int32_t * __restrict output_curve_b = nullptr);
    void setTimingEnabled(bool enabled) { m_timing_enabled = enabled; }
    // true: one parallel region, rows then column blocks, with the vertical
    // passes column-parallel; bit-exact to the legacy passes.
    // false: the legacy passes with the serial vertical pair. The class
    // defaults to true; the product wrapper (rbf_wrapper.cpp) defaults to false.
    void setParallelVertical(bool enabled) { m_parallel_vertical = enabled; }
    // Team size cap of the parallel path (0: OpenMP's default). The result
    // does not depend on it.
    void setMaxThreads(int threads) { m_max_threads = threads; }
    RBFilterPlainTiming lastTiming() const { return m_last_timing; }
    // Raw vertical-pass state of the last filter() call (bit-exactness tests).
    const float* downPassColor() const { return m_down_pass_color; }
    const float* downPassFactor() const { return m_down_pass_factor; }
    const float* upPassColor() const { return m_up_pass_color; }
    const float* upPassFactor() const { return m_up_pass_factor; }
};
