// Shared by test_gpu_dualiso_preview_scale.cpp and test_cpu_dualiso_preview_scale.cpp:
// the 4-row ISO-mesh metrics on an interleaved 8-bit RGB preview, and the CPU reduced
// recon without the reduced ISO notch (CPU-DUALISO-REDUCED-ISO-NOTCH-1).
#pragma once

#include "../../src/mlv/video_mlv.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace dualiso_mesh_metrics {

// L1: the 4-row ISO residual, row means correlated with the period-4 basis.
inline double rowPeriod4Energy(const std::vector<uint8_t> & rgb, int w, int h)
{
    std::vector<double> rows(static_cast<size_t>(h), 0.0);
    for (int y = 0; y < h; ++y)
    {
        double s = 0.0;
        for (int x = 0; x < w * 3; ++x) s += rgb[static_cast<size_t>(y) * w * 3 + x];
        rows[static_cast<size_t>(y)] = s / (w * 3.0);
    }
    // Remove the local mean (5-row box), then correlate with the period-4 basis.
    double re = 0.0, im = 0.0;
    int n = 0;
    for (int y = 2; y < h - 2; ++y)
    {
        const double local = (rows[y - 2] + rows[y - 1] + rows[y] + rows[y + 1] + rows[y + 2]) / 5.0;
        const double v = rows[static_cast<size_t>(y)] - local;
        re += v * std::cos(2.0 * 3.14159265358979 * y / 4.0);
        im += v * std::sin(2.0 * 3.14159265358979 * y / 4.0);
        ++n;
    }
    return n > 0 ? std::sqrt(re * re + im * im) / n : 0.0;
}

// L2: the same estimator per column on the per-pixel channel mean, averaged
// across columns (catches the 2-D dots a row average would hide).
inline double columnPeriod4Energy(const std::vector<uint8_t> & rgb, int w, int h)
{
    double sum = 0.0;
    std::vector<double> col(static_cast<size_t>(h), 0.0);
    for (int x = 0; x < w; ++x)
    {
        for (int y = 0; y < h; ++y)
        {
            const size_t i = (static_cast<size_t>(y) * w + x) * 3;
            col[static_cast<size_t>(y)] = (rgb[i] + rgb[i + 1] + rgb[i + 2]) / 3.0;
        }
        double re = 0.0, im = 0.0;
        int n = 0;
        for (int y = 2; y < h - 2; ++y)
        {
            const double local = (col[y - 2] + col[y - 1] + col[y] + col[y + 1] + col[y + 2]) / 5.0;
            const double v = col[static_cast<size_t>(y)] - local;
            re += v * std::cos(2.0 * 3.14159265358979 * y / 4.0);
            im += v * std::sin(2.0 * 3.14159265358979 * y / 4.0);
            ++n;
        }
        sum += n > 0 ? std::sqrt(re * re + im * im) / n : 0.0;
    }
    return w > 0 ? sum / w : 0.0;
}

// L3: lag-4 vertical luma detail, mean |L(x,y+4) - L(x,y)| (blind to period 4).
inline double lag4VerticalLumaDetail(const std::vector<uint8_t> & rgb, int w, int h)
{
    double sum = 0.0;
    size_t n = 0;
    auto luma = [&](int x, int y) {
        const size_t i = (static_cast<size_t>(y) * w + x) * 3;
        return 0.299 * rgb[i] + 0.587 * rgb[i + 1] + 0.114 * rgb[i + 2];
    };
    for (int y = 0; y + 4 < h; ++y)
    {
        for (int x = 0; x < w; ++x)
        {
            sum += std::fabs(luma(x, y + 4) - luma(x, y));
            ++n;
        }
    }
    return n ? sum / static_cast<double>(n) : 0.0;
}

inline double channelMean(const std::vector<uint8_t> & rgb, int w, int h, int c)
{
    double sum = 0.0;
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x) sum += rgb[(static_cast<size_t>(y) * w + x) * 3 + c];
    return sum / (static_cast<double>(w) * h);
}

// The CPU reduced recon (mlvDualIsoPreviewScaleReconRun) without the reduced ISO
// notch: the plan's shrink, then the with-dims recon with the route's flags minus
// LLRP_WITH_DIMS_REDUCED_ISO_NOTCH. `raw` is a full-resolution decode (consumed).
// Empty on any refusal.
inline std::vector<uint16_t> unnotchedCpuReducedRecon(mlvObject_t * video,
                                                      const mlvDualIsoPreviewScaleRecon_t & plan,
                                                      std::vector<uint16_t> raw)
{
    std::vector<uint16_t> reduced(static_cast<size_t>(plan.reducedWidth)
                                  * static_cast<size_t>(plan.reducedHeight));
    llrawprocWorkerState_t worker;
    llrpInitWorkerState(&worker);
    const int shrunk = mlvDualIsoPreviewScaleReconShrink(video, &plan, raw.data(), reduced.data(),
                                                         &worker, 1, nullptr, nullptr);
    const int ok = shrunk > 0
        && applyLLRawProcObjectWorker_with_dims(video, reduced.data(), reduced.size() * sizeof(uint16_t),
                                                plan.reducedWidth, plan.reducedHeight, &worker,
                                                LLRP_WITH_DIMS_FULLRES_FIXES_APPLIED
                                                | LLRP_WITH_DIMS_NO_PUBLISH);
    llrpFreeWorkerState(&worker);
    if (!ok) reduced.clear();
    return reduced;
}

} // namespace dualiso_mesh_metrics
