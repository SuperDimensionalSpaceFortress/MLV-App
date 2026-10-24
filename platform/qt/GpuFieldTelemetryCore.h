/*!
 * \file GpuFieldTelemetryCore.h
 * \brief The pure half of GPU field telemetry (GPU-FIELD-TELEMETRY-1): the
 *        per-stage aggregate, the fault record, the JSONL line text and the
 *        log rotation. No Qt and no CUDA, so console_tests compiles it
 *        directly (tests/console/test_gpu_field_telemetry.cpp).
 *
 * The log is local-first and ON by default; MLVAPP_GPU_TELEMETRY=0|off|false|no
 * turns it off. Upload is not implemented: every session line says
 * "upload":"none". Lines carry no paths, user names, host names, camera serials
 * or event message text: every free-text field goes through sanitize(), which
 * maps '/' and '\\' to '|' and drops quotes and control characters, so no line
 * ever holds a path separator or a JSON escape. A clip is identified only by
 * clip_hash (first 12 hex of SHA-256 of the basename, computed by the caller).
 *
 * Stage statistics reuse the CUDA launch timer's host-only aggregate
 * (IgpuLtStats, igpu_lt_stats_add with min_ms = kWarnMs, so over_min is the
 * over-500 ms count) plus its fixed log-bucket histogram for p95. Stage lines
 * are cumulative for the session: the last line per (session_id, dll, stage)
 * wins.
 */
#ifndef GPU_FIELD_TELEMETRY_CORE_H
#define GPU_FIELD_TELEMETRY_CORE_H

#include "../../tools/gpu/backend/igpu_launch_timing_format.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <string>
#include <system_error>
#include <vector>

namespace GpuFieldTelemetryCore
{

constexpr const char * kSchema = "gpu-field-telemetry.v1";
constexpr const char * kEnvEnable = "MLVAPP_GPU_TELEMETRY";
constexpr const char * kLogFileName = "gpu-field-telemetry.jsonl";
constexpr double kWarnMs = 500.0;
constexpr std::uintmax_t kRotateBytes = 1024u * 1024u;
constexpr int kRotateKeep = 3;
constexpr std::size_t kMaxFaults = 64;

/* Exit join: session end waits at most kSessionEndWaitMs for the after-fault
 * worker, and any System-log query (the worker's or session end's own) has a
 * total deadline shorter than that wait, so a worker woken at session end has
 * finished before the wait expires. */
constexpr long long kSessionEndWaitMs = 2000;
constexpr long long kSystemLogDeadlineMs = 1500;
constexpr long long kEvtNextMaxWaitMs = 1000;
static_assert(kSystemLogDeadlineMs < kSessionEndWaitMs, "the System-log query must end inside the session-end wait");
/* r6: session end's terminal drain waits at most this long for each backend's
 * lock (a call in flight keeps its own codes), and never past the budget. */
constexpr long long kTerminalDrainWaitMs = 250;
static_assert(kTerminalDrainWaitMs < kSessionEndWaitMs, "the terminal drain must fit the session-end budget");

/* The EvtNext wait for the next batch: at most kEvtNextMaxWaitMs and never past
 * the query's total deadline (a passed deadline clamps to no wait). */
inline unsigned long evtNextWaitMs(long long remainingMs)
{
    return static_cast<unsigned long>(std::clamp(remainingMs, 0LL, kEvtNextMaxWaitMs));
}

/* ON unless the value is 0|off|false|no (case-insensitive, trimmed); unset or
 * empty is ON (the log is local-first and on by default). */
inline bool parseEnabled(const char * value)
{
    if (!value) return true;
    while (*value == ' ' || *value == '\t') ++value;
    std::size_t n = std::strlen(value);
    while (n > 0 && (value[n - 1] == ' ' || value[n - 1] == '\t' || value[n - 1] == '\r' || value[n - 1] == '\n')) --n;
    if (n == 0 || n > 5) return true;
    char lower[6] = {0, 0, 0, 0, 0, 0};
    for (std::size_t i = 0; i < n; ++i)
    {
        const char c = value[i];
        lower[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
    }
    return !(std::strcmp(lower, "0") == 0 || std::strcmp(lower, "off") == 0
             || std::strcmp(lower, "false") == 0 || std::strcmp(lower, "no") == 0);
}

/* Device loss: cudaErrorIllegalAddress (700), cudaErrorLaunchTimeout (702),
 * cudaErrorLaunchFailure (719), and the "unknown" rule: cudaErrorUnknown (999),
 * which is what a context reset by the driver (TDR) commonly returns. Other
 * sticky 7xx codes are not counted here; GPU-DEVICE-LOSS-SELF-HEAL-1 reuses this
 * and may widen it. */
inline bool isDeviceLossCode(int cudaCode)
{
    return cudaCode == 700 || cudaCode == 702 || cudaCode == 719 || cudaCode == 999;
}

/* Free text -> one safe JSON string body: '/' and '\\' become '|', '"' becomes
 * '\'', control and non-ASCII bytes become ' ' / '?', truncated to maxLen. */
inline std::string sanitize(const char * text, std::size_t maxLen = 160)
{
    std::string out;
    if (!text) return out;
    for (const char * p = text; *p && out.size() < maxLen; ++p)
    {
        const unsigned char c = (unsigned char)*p;
        if (c == '/' || c == '\\') out.push_back('|');
        else if (c == '"') out.push_back('\'');
        else if (c < 0x20 || c == 0x7f) out.push_back(' ');
        else if (c >= 0x80) out.push_back('?');
        else out.push_back((char)c);
    }
    return out;
}

inline std::string sanitize(const std::string & text, std::size_t maxLen = 160)
{
    return sanitize(text.c_str(), maxLen);
}

inline std::string quoted(const std::string & text)
{
    return "\"" + sanitize(text) + "\"";
}

inline std::string number(double value)
{
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.3f", value);
    return buf;
}

inline std::string number(unsigned long long value)
{
    return std::to_string(value);
}

/* "CUDA / NVIDIA GeForce RTX 3060 Laptop GPU" (igpu_recon_describe) or
 * "CUDA AMaZE / ..." -> the device name alone. Anything else is kept as is. */
inline std::string gpuModelFromCudaDescribe(const char * describe)
{
    if (!describe) return std::string();
    const char * slash = std::strstr(describe, " / ");
    if (slash && std::strncmp(describe, "CUDA", 4) == 0) return sanitize(slash + 3);
    return sanitize(describe);
}

/* GL_VERSION "4.6.0 NVIDIA 610.74" -> "610.74"; any other vendor's string is
 * kept raw (sanitized). */
inline std::string driverFromGlVersion(const char * glVersion)
{
    if (!glVersion) return std::string();
    const char * nv = std::strstr(glVersion, "NVIDIA ");
    if (nv)
    {
        nv += 7;
        std::string ver;
        while (*nv && *nv != ' ') ver.push_back(*nv++);
        if (!ver.empty()) return sanitize(ver);
    }
    return sanitize(glVersion);
}

/* GL_RESET_NOTIFICATION_STRATEGY, read only on a KHR/ARB robustness context. */
constexpr int kGlLoseContextOnReset = 0x8252;
constexpr int kGlNoResetNotification = 0x8261;
constexpr int kGlGuiltyContextReset = 0x8253;
constexpr int kGlInnocentContextReset = 0x8254;
constexpr int kGlUnknownContextReset = 0x8255;

inline std::string glResetStrategyName(bool hasRobustness, int strategy)
{
    if (!hasRobustness) return "unsupported";
    if (strategy == kGlLoseContextOnReset) return "lose_context";
    if (strategy == kGlNoResetNotification) return "none";
    return "unknown";
}

inline std::string glResetStatusName(int status)
{
    if (status == 0) return "no_error";
    if (status == kGlGuiltyContextReset) return "guilty_context_reset";
    if (status == kGlInnocentContextReset) return "innocent_context_reset";
    if (status == kGlUnknownContextReset) return "unknown_context_reset";
    return "unknown";
}

struct StageEntry
{
    std::string dll;
    std::string stage;
    IgpuLtStats stats;
    IgpuLtHistogram hist;
};

/* Per (dll, stage) aggregate. add() is one map lookup plus a stats and a
 * histogram add; the caller serialises it. */
class StageMap
{
public:
    /* ms < 0 is a call with no duration (a failed call); err != 0 counts it in
     * errors. */
    void add(const char * dll, const char * stage, double ms, int err)
    {
        const std::string d = dll ? dll : "?";
        const std::string s = stage ? stage : "?";
        const std::string key = d + "|" + s;
        auto it = m_.find(key);
        if (it == m_.end())
        {
            StageEntry fresh;
            fresh.dll = d;
            fresh.stage = s;
            std::memset(&fresh.stats, 0, sizeof(fresh.stats));
            std::memset(&fresh.hist, 0, sizeof(fresh.hist));
            it = m_.emplace(key, fresh).first;
        }
        StageEntry & e = it->second;
        IgpuLtRecord r;
        std::memset(&r, 0, sizeof(r));
        r.dll = e.dll.c_str();
        r.kind = "stage";
        r.stage = e.stage.c_str();
        r.tile_top = IGPU_LT_NO_TILE;
        r.tile_left = IGPU_LT_NO_TILE;
        r.ms = ms;
        r.err = err;
        igpu_lt_stats_add(&e.stats, r, kWarnMs);
        igpu_lt_hist_add(&e.hist, ms);
    }

    const std::map<std::string, StageEntry> & entries() const { return m_; }
    void clear() { m_.clear(); }

private:
    std::map<std::string, StageEntry> m_;
};

inline double stageP95(const StageEntry & e)
{
    if (!e.stats.has_max) return 0.0;
    return igpu_lt_hist_quantile(e.hist, 0.95, e.stats.max_ms);
}

struct Fault
{
    std::string dll;
    std::string stage;
    int rc = 0;
    int cudaCode = 0;
    std::string cudaName;
    std::string site;
    double utcMs = 0.0;             /* when noted (the DLL's utc_ms when it gave one) */
    double deliveredUtcMs = 0.0;    /* when the app recorded it (a drain can be late) */
    std::string role = "primary";  /* r5: primary | secondary | orphan */
    unsigned long long dropped = 0; /* r5: DLL queue drops reported with this record */
};

struct SessionInfo
{
    std::string sessionId;
    std::string startUtc;
    std::string appBuild;
    std::string osBuild;
    std::string gpuModel;
    std::string gpuDriver;
    std::string glRenderer;
    std::string glVendor;
    std::string glVersion;
    std::string glResetStrategy;
    std::string glResetStatus;
    std::string launchTimingEnv;
};

struct ClipInfo
{
    int width = 0;
    int height = 0;
    int bitsPerPixel = 0;
    bool compressed = false;
    bool dualIsoGpu = false;
    std::string clipHash;
};

struct SystemLogEvents
{
    unsigned long long count = 0;
    std::string firstUtc;
    std::string lastUtc;
};

struct SystemLogResult
{
    std::string status; /* ok | access_denied | unavailable | timeout | interrupted */
    SystemLogEvents nvlddmkm153;
    SystemLogEvents display4101;
};

inline std::string linePrefix(const char * kind, const std::string & sessionId, const std::string & utc)
{
    return std::string("{\"schema\":\"") + kSchema + "\",\"kind\":\"" + kind
        + "\",\"session_id\":" + quoted(sessionId) + ",\"utc\":" + quoted(utc);
}

inline std::string formatHeader(const SessionInfo & s, const std::string & utc)
{
    return linePrefix("session", s.sessionId, utc)
        + ",\"start_utc\":" + quoted(s.startUtc)
        + ",\"app_build\":" + quoted(s.appBuild)
        + ",\"os_build\":" + quoted(s.osBuild)
        + ",\"gpu_model\":" + quoted(s.gpuModel)
        + ",\"gpu_driver\":" + quoted(s.gpuDriver)
        + ",\"gl_renderer\":" + quoted(s.glRenderer)
        + ",\"gl_vendor\":" + quoted(s.glVendor)
        + ",\"gl_version\":" + quoted(s.glVersion)
        + ",\"gl_reset_strategy\":" + quoted(s.glResetStrategy)
        + ",\"gl_reset_status\":" + quoted(s.glResetStatus)
        + ",\"launch_timing_env\":" + quoted(s.launchTimingEnv)
        + ",\"upload\":\"none\"}";
}

/* dropped_rotation_failed: lines this session did not write because the live
 * file was at the cap and rotation failed (writeBounded). */
inline std::string formatFlush(const std::string & sessionId, const std::string & utc,
                               const char * reason, unsigned long long seq,
                               unsigned long long faultsDropped,
                               unsigned long long droppedRotationFailed = 0)
{
    return linePrefix("flush", sessionId, utc)
        + ",\"reason\":" + quoted(reason ? reason : "")
        + ",\"seq\":" + number(seq)
        + ",\"faults_dropped\":" + number(faultsDropped)
        + ",\"dropped_rotation_failed\":" + number(droppedRotationFailed) + "}";
}

inline std::string formatClip(const std::string & sessionId, const std::string & utc, const ClipInfo & c)
{
    return linePrefix("clip", sessionId, utc)
        + ",\"width\":" + std::to_string(c.width)
        + ",\"height\":" + std::to_string(c.height)
        + ",\"bits_per_pixel\":" + std::to_string(c.bitsPerPixel)
        + ",\"compressed\":" + (c.compressed ? "true" : "false")
        + ",\"dual_iso_gpu\":" + (c.dualIsoGpu ? "true" : "false")
        + ",\"clip_hash\":" + quoted(c.clipHash) + "}";
}

inline std::string formatStage(const std::string & sessionId, const std::string & utc, const StageEntry & e)
{
    return linePrefix("stage", sessionId, utc)
        + ",\"dll\":" + quoted(e.dll)
        + ",\"stage\":" + quoted(e.stage)
        + ",\"count\":" + number(e.stats.count)
        + ",\"max_ms\":" + number(e.stats.has_max ? e.stats.max_ms : 0.0)
        + ",\"p95_ms\":" + number(stageP95(e))
        + ",\"over_500ms\":" + number(e.stats.over_min)
        + ",\"errors\":" + number(e.stats.errors)
        + ",\"sum_ms\":" + number(e.stats.sum_ms) + "}";
}

inline std::string formatFault(const std::string & sessionId, const std::string & utc, const Fault & f)
{
    return linePrefix("fault", sessionId, utc)
        + ",\"dll\":" + quoted(f.dll)
        + ",\"stage\":" + quoted(f.stage)
        + ",\"rc\":" + std::to_string(f.rc)
        + ",\"cuda_code\":" + std::to_string(f.cudaCode)
        + ",\"cuda_name\":" + quoted(f.cudaName)
        + ",\"site\":" + quoted(f.site)
        + ",\"fault_utc_ms\":" + number(f.utcMs)
        + ",\"fault_delivered_utc_ms\":" + number(f.deliveredUtcMs)
        + ",\"device_loss\":" + (isDeviceLossCode(f.cudaCode) ? "true" : "false")
        + ",\"role\":" + quoted(f.role)
        + ",\"dropped\":" + number(f.dropped) + "}";
}

inline std::string formatSystemLogEvents(const SystemLogEvents & e)
{
    return "{\"count\":" + number(e.count)
        + ",\"first_utc\":" + quoted(e.firstUtc)
        + ",\"last_utc\":" + quoted(e.lastUtc) + "}";
}

inline std::string formatSystemLog(const std::string & sessionId, const std::string & utc,
                                   const char * reason, const SystemLogResult & r)
{
    return linePrefix("system_log", sessionId, utc)
        + ",\"reason\":" + quoted(reason ? reason : "")
        + ",\"status\":" + quoted(r.status)
        + ",\"nvlddmkm_153\":" + formatSystemLogEvents(r.nvlddmkm153)
        + ",\"display_4101\":" + formatSystemLogEvents(r.display4101) + "}";
}

inline std::string formatRecovery(const std::string & sessionId, const std::string & utc,
                                  const char * what, const char * outcome)
{
    return linePrefix("recovery", sessionId, utc)
        + ",\"what\":" + quoted(what ? what : "")
        + ",\"outcome\":" + quoted(outcome ? outcome : "") + "}";
}

/* <live>.<index>, e.g. gpu-field-telemetry.jsonl.2 */
inline std::filesystem::path rotatedPath(const std::filesystem::path & live, int index)
{
    std::filesystem::path p = live;
    p += "." + std::to_string(index);
    return p;
}

/* Called before each flush's append. When the live file is at or over capBytes
 * it shifts .1 -> .2 -> ... -> .keep (dropping the oldest) and moves the live
 * file to .1; the next append then opens a fresh live file. Returns true if it
 * rotated. */
inline bool rotateIfNeeded(const std::filesystem::path & live, std::uintmax_t capBytes, int keep)
{
    std::error_code ec;
    if (keep < 1) return false;
    const std::uintmax_t size = std::filesystem::file_size(live, ec);
    if (ec || size < capBytes) return false;
    std::filesystem::remove(rotatedPath(live, keep), ec);
    for (int i = keep - 1; i >= 1; --i)
    {
        const std::filesystem::path from = rotatedPath(live, i);
        if (std::filesystem::exists(from, ec)) std::filesystem::rename(from, rotatedPath(live, i + 1), ec);
    }
    std::filesystem::rename(live, rotatedPath(live, 1), ec);
    return !ec;
}

/* Opens the live file for append, writes each line plus '\n', closes it. */
inline bool appendLines(const std::filesystem::path & live, const std::vector<std::string> & lines)
{
    std::ofstream out(live, std::ios::out | std::ios::app | std::ios::binary);
    if (!out) return false;
    for (const std::string & line : lines) out << line << '\n';
    out.flush();
    return (bool)out;
}

enum class WriteOutcome
{
    Written,
    DroppedRotationFailed, /* the live file is still at the cap: nothing appended */
    WriteFailed            /* open or write failed (unwritable dir, full disk) */
};

/* One flush's write. Rotates first; if the live file is still at or over the
 * cap (a rotation that failed, e.g. a generation held open without
 * FILE_SHARE_DELETE) it appends nothing, so the live file never exceeds
 * cap + one flush and the on-disk total stays under (keep + 1) x (cap + one
 * flush) however often rotation fails. */
inline WriteOutcome writeBounded(const std::filesystem::path & live, const std::vector<std::string> & lines,
                                 std::uintmax_t capBytes, int keep)
{
    rotateIfNeeded(live, capBytes, keep);
    std::error_code ec;
    const std::uintmax_t size = std::filesystem::file_size(live, ec);
    if (!ec && size >= capBytes) return WriteOutcome::DroppedRotationFailed;
    return appendLines(live, lines) ? WriteOutcome::Written : WriteOutcome::WriteFailed;
}

constexpr std::size_t kMaxPendingLines = 64;

/* What the log has not persisted yet: faults (at most kMaxFaults), extra lines
 * such as recovery (at most kMaxPendingLines) and the current clip. A flush
 * copies them into its lines (takeInto) and removes them only once the write
 * succeeded (commit); a dropped or failed write keeps them for the next flush.
 * The caller serialises flushes, so only appends happen between take and
 * commit. */
struct Unwritten
{
    std::vector<Fault> faults;
    unsigned long long faultsDropped = 0;
    std::vector<std::string> lines;
    bool haveClip = false;
    ClipInfo clip;
    unsigned long long clipSeq = 0;
    unsigned long long clipWrittenSeq = 0;
    unsigned long long droppedRotationFailed = 0;

    void addFault(const Fault & f)
    {
        if (faults.size() < kMaxFaults) faults.push_back(f);
        else ++faultsDropped;
    }
    void addLine(std::string line)
    {
        if (lines.size() < kMaxPendingLines) lines.push_back(std::move(line));
    }
    void setClip(const ClipInfo & c)
    {
        clip = c;
        haveClip = true;
        ++clipSeq;
    }
};

struct Taken
{
    std::size_t faults = 0;
    std::size_t lines = 0;
    unsigned long long clipSeq = 0;
};

inline Taken takeInto(const Unwritten & u, const std::string & sessionId, const std::string & utc,
                      std::vector<std::string> & out)
{
    Taken t;
    t.clipSeq = u.clipWrittenSeq;
    if (u.haveClip && u.clipSeq != u.clipWrittenSeq)
    {
        out.push_back(formatClip(sessionId, utc, u.clip));
        t.clipSeq = u.clipSeq;
    }
    for (const Fault & f : u.faults) out.push_back(formatFault(sessionId, utc, f));
    t.faults = u.faults.size();
    out.insert(out.end(), u.lines.begin(), u.lines.end());
    t.lines = u.lines.size();
    return t;
}

/* lineCount: the lines this flush tried to write (counted when dropped). */
inline void commit(Unwritten & u, const Taken & t, WriteOutcome outcome, std::size_t lineCount)
{
    if (outcome != WriteOutcome::Written)
    {
        if (outcome == WriteOutcome::DroppedRotationFailed) u.droppedRotationFailed += lineCount;
        return;
    }
    u.faults.erase(u.faults.begin(), u.faults.begin() + static_cast<std::ptrdiff_t>(std::min(t.faults, u.faults.size())));
    u.lines.erase(u.lines.begin(), u.lines.begin() + static_cast<std::ptrdiff_t>(std::min(t.lines, u.lines.size())));
    if (t.clipSeq > u.clipWrittenSeq) u.clipWrittenSeq = t.clipSeq;
}

} // namespace GpuFieldTelemetryCore

#endif // GPU_FIELD_TELEMETRY_CORE_H
