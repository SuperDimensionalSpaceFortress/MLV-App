/*!
 * \file GpuDeviceLossBreakerCore.h
 * \brief The pure half of the GPU device-loss circuit breaker
 *        (GPU-DEVICE-LOSS-SELF-HEAL-1, owner spec r2 Part 4): the window and
 *        threshold, the fault-injection spec, the profile key, the recent-loss
 *        record and its text form, the identity-change rule and the notice
 *        texts. No Qt and no CUDA, so console_tests compiles it directly
 *        (tests/console/test_gpu_device_loss_breaker.cpp).
 *
 * Rule: two device losses within kWindowMs on the same GPU + driver + clip
 * profile set the persisted safe-start flag. The notices never ask the user to
 * reboot or restart (owner spec r2); the event is recorded in telemetry.
 */
#ifndef GPU_DEVICE_LOSS_BREAKER_CORE_H
#define GPU_DEVICE_LOSS_BREAKER_CORE_H

#include "GpuFieldTelemetryCore.h"

#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace GpuDeviceLossBreakerCore
{

constexpr long long kWindowMs = 600000;
constexpr int kThreshold = 2;
constexpr std::size_t kMaxRecent = 8;

constexpr const char * kEnvFaultInject = "MLVAPP_GPU_FAULT_INJECT";
constexpr const char * kEnvSafeStart = "MLVAPP_GPU_SAFE_START";
constexpr const char * kFaultInjectSite = "MLVAPP_GPU_FAULT_INJECT";

constexpr const char * kKeySafeStartEnabled = "GpuDeviceLoss/SafeStartEnabled";
constexpr const char * kKeySafeStartGlRenderer = "GpuDeviceLoss/SafeStartGlRenderer";
constexpr const char * kKeySafeStartDriver = "GpuDeviceLoss/SafeStartDriver";
constexpr const char * kKeySafeStartClipProfile = "GpuDeviceLoss/SafeStartClipProfile";
constexpr const char * kKeySafeStartSetUtc = "GpuDeviceLoss/SafeStartSetUtc";
constexpr const char * kKeyRecentLosses = "GpuDeviceLoss/RecentLosses";
/* Every SafeStart* key: what a clear removes (RecentLosses is kept). */
constexpr const char * kSafeStartKeys[] = {kKeySafeStartEnabled, kKeySafeStartGlRenderer, kKeySafeStartDriver,
                                           kKeySafeStartClipProfile, kKeySafeStartSetUtc};

/* %1 = the CUDA code name. */
constexpr const char * kNoticeSession =
    "GPU playback stopped after a graphics driver fault (%1). Playback continues on the CPU for this session.";
constexpr const char * kNoticeFlagSet =
    "GPU playback stopped after a graphics driver fault (%1). Playback continues on the CPU. Faults repeated on "
    "this GPU and driver, so the next launch also starts on the CPU until the driver changes or the CUDA playback "
    "preset is turned on again.";
constexpr const char * kNoticeStartup =
    "Playback starts on the CPU after repeated graphics driver faults on this GPU and driver. Turn on the CUDA "
    "playback preset (Performance profiling) to try the GPU again.";

inline const char * cudaNameForCode(int code)
{
    switch (code)
    {
    case 700: return "cudaErrorIllegalAddress";
    case 702: return "cudaErrorLaunchTimeout";
    case 719: return "cudaErrorLaunchFailure";
    case 999: return "cudaErrorUnknown";
    default: return "cudaErrorUnknown";
    }
}

/* "<template>" with %1 replaced by `codeName`. */
inline std::string noticeText(const char * tmpl, const std::string & codeName)
{
    std::string out = tmpl ? tmpl : "";
    const std::size_t at = out.find("%1");
    if (at != std::string::npos) out.replace(at, 2, codeName);
    return out;
}

struct FaultInject
{
    bool armed = false;
    int code = 0;
    long long n = 0; /* the n-th successful run stage (1-based) */
};

/* "<code>@<n>": code one of 700, 702, 719, 999; n >= 1. Anything else
 * (unset, empty, junk, another code, n < 1) is disarmed. */
inline FaultInject parseFaultInject(const char * value)
{
    FaultInject disarmed;
    if (!value) return disarmed;
    while (*value == ' ' || *value == '\t') ++value;
    const char * at = std::strchr(value, '@');
    if (!at || at == value) return disarmed;
    char * end = nullptr;
    const long code = std::strtol(value, &end, 10);
    if (end != at) return disarmed;
    if (code != 700 && code != 702 && code != 719 && code != 999) return disarmed;
    const char * nText = at + 1;
    if (*nText < '0' || *nText > '9') return disarmed;
    const long long n = std::strtoll(nText, &end, 10);
    while (*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n') ++end;
    if (*end != '\0' || n < 1) return disarmed;
    FaultInject armed;
    armed.armed = true;
    armed.code = static_cast<int>(code);
    armed.n = n;
    return armed;
}

inline std::string keyPart(const std::string & text)
{
    const std::string s = GpuFieldTelemetryCore::sanitize(text, 96);
    return s.empty() ? std::string("unknown") : s;
}

/* The clip half of the key; "unknown" before any clip was opened (w or h <= 0). */
inline std::string clipProfile(int width, int height, int bitsPerPixel, bool compressed)
{
    if (width <= 0 || height <= 0) return "unknown";
    return std::to_string(width) + "x" + std::to_string(height) + "x" + std::to_string(bitsPerPixel)
        + (compressed ? "c" : "u");
}

/* GPU + driver + clip profile; every part sanitized, an empty part "unknown". */
inline std::string profileKey(const std::string & glRenderer, const std::string & driver, int width, int height,
                              int bitsPerPixel, bool compressed)
{
    return keyPart(glRenderer) + ";" + keyPart(driver) + ";"
        + GpuFieldTelemetryCore::sanitize(clipProfile(width, height, bitsPerPixel, compressed), 64);
}

struct Loss
{
    long long utcMs = 0;
    std::string key;
};

struct RecordResult
{
    int sameProfileInWindow = 0; /* including the loss just recorded */
    bool setSafeStart = false;
};

/* Drops entries older than kWindowMs before `loss`, appends it, keeps the
 * newest kMaxRecent. */
inline RecordResult recordLoss(std::vector<Loss> & recent, const Loss & loss)
{
    std::vector<Loss> kept;
    for (const Loss & e : recent)
        if (loss.utcMs - e.utcMs <= kWindowMs) kept.push_back(e);
    kept.push_back(loss);
    if (kept.size() > kMaxRecent) kept.erase(kept.begin(), kept.end() - static_cast<std::ptrdiff_t>(kMaxRecent));
    recent.swap(kept);
    RecordResult r;
    for (const Loss & e : recent)
        if (e.key == loss.key) ++r.sameProfileInWindow;
    r.setSafeStart = r.sameProfileInWindow >= kThreshold;
    return r;
}

/* "utcMs|key" (the key may itself hold '|': only the first one separates). */
inline std::string serialize(const Loss & loss)
{
    return std::to_string(loss.utcMs) + "|" + loss.key;
}

inline bool parse(const std::string & text, Loss * out)
{
    const std::size_t bar = text.find('|');
    if (bar == std::string::npos || bar == 0 || bar + 1 >= text.size()) return false;
    long long ms = 0;
    for (std::size_t i = 0; i < bar; ++i)
    {
        if (text[i] < '0' || text[i] > '9' || i >= 18) return false;
        ms = ms * 10 + (text[i] - '0');
    }
    if (out)
    {
        out->utcMs = ms;
        out->key = text.substr(bar + 1);
    }
    return true;
}

/* The flag clears when the GL renderer or driver differ from those it was set
 * on. An empty current value (identity not known yet) is never a change. */
inline bool identityChanged(const std::string & storedRenderer, const std::string & storedDriver,
                            const std::string & currentRenderer, const std::string & currentDriver)
{
    if (currentRenderer.empty() || currentDriver.empty()) return false;
    return storedRenderer != currentRenderer || storedDriver != currentDriver;
}

} // namespace GpuDeviceLossBreakerCore

#endif // GPU_DEVICE_LOSS_BREAKER_CORE_H
