/* igpu_launch_budget.h - the per-launch time budget for the two CUDA backend
 * DLLs (GPU-LAUNCH-BUDGET-TILING-1; owner spec event153 r2, Part 2: keep every
 * GPU launch far under the ~2 s Windows TDR window, aim <= 100 ms per launch).
 * Host-only, like igpu_launch_timing_format.h: no CUDA and no Qt, so
 * console_tests compiles it directly (tests/console/test_gpu_launch_budget.cpp)
 * and a bench lane can judge a run's stderr with the same rules.
 *
 * Input: the lines igpu_launch_timing.cuh writes (formats in
 * igpu_launch_timing_format.h):
 *   igpu_launch_timing dll=.. kind=.. stage=.. ... w=.. h=.. ms=..
 *   igpu_launch_timing_summary dll=.. kind=.. stage=.. ... max_ms=.. max_w=.. max_h=..
 * Every other line (config, drops, any other stderr) is skipped, and so is any
 * key=value field this parser does not use, so new fields cost nothing here.
 *
 * Verdict, per (dll, kind, stage), on the longest duration seen:
 *   kind=kernel over budget -> VIOLATION: one GPU launch (cudaEvent-timed) ran
 *     longer than the budget.
 *   kind=sync over budget -> HOST_WAIT: host time around a synchronising call
 *     (a blocking memcpy, an event wait, a GL map). It is not a launch, and the
 *     GPU need not be busy for any of it, so it is listed and never a violation.
 * "Over" is strictly greater: exactly the budget passes.
 */
#ifndef IGPU_LAUNCH_BUDGET_H
#define IGPU_LAUNCH_BUDGET_H

#include <cerrno>
#include <cstdlib>
#include <map>
#include <string>
#include <vector>

#define IGPU_LT_LAUNCH_BUDGET_MS 100.0

/* The largest frame a whole-frame launch is budgeted for. No app-level cap
 * exists: llrawproc.c only rejects frames wider or taller than 65535. 8192x4320
 * is the largest size the repo exercises (tools/gpu/recon-probe-sweep.ps1, the
 * 8K row of its resolution sweep). */
#define IGPU_LT_BUDGET_MAX_FRAME_W 8192
#define IGPU_LT_BUDGET_MAX_FRAME_H 4320

struct IgpuLbRecord
{
    std::string dll;
    std::string kind;   /* "kernel" | "sync" */
    std::string stage;
    double ms;          /* < 0: the line has no duration (ms=na / max_ms=na) */
    int width;          /* frame dims; 0 when the line has none */
    int height;
    int summary;        /* 1 = from a summary line (its max_* fields) */
};

struct IgpuLbStageMax
{
    std::string dll;
    std::string kind;
    std::string stage;
    double max_ms;
    int width;          /* the frame dims of the longest record */
    int height;
};

struct IgpuLbVerdict
{
    std::vector<IgpuLbStageMax> stages;      /* every stage with a duration, sorted by dll, kind, stage */
    std::vector<IgpuLbStageMax> violations;  /* kind=kernel over budget */
    std::vector<IgpuLbStageMax> host_waits;  /* kind=sync over budget */
};

/* Whole-string number parsers; 0 on anything else. */
static inline int igpu_lb_parse_double(const std::string & text, double * out)
{
    if (text.empty()) return 0;
    char * end = nullptr;
    errno = 0;
    const double v = std::strtod(text.c_str(), &end);
    if (end != text.c_str() + text.size() || errno != 0 || !(v == v)) return 0;
    *out = v;
    return 1;
}

static inline int igpu_lb_parse_int(const std::string & text, int * out)
{
    if (text.empty()) return 0;
    char * end = nullptr;
    errno = 0;
    const long v = std::strtol(text.c_str(), &end, 10);
    if (end != text.c_str() + text.size() || errno != 0 || v < 0 || v > 2147483647L) return 0;
    *out = (int)v;
    return 1;
}

/* 1 and fills *out for an igpu_launch_timing or igpu_launch_timing_summary
 * line that names its dll, kind and stage; 0 for any other line. A duration
 * that is "na" or unparseable leaves ms < 0. */
static inline int igpu_lb_parse_line(std::string line, IgpuLbRecord * out)
{
    while (!line.empty() && (line[line.size() - 1] == '\r' || line[line.size() - 1] == '\n')) line.erase(line.size() - 1);
    std::vector<std::string> tokens;
    std::size_t pos = 0;
    while (pos < line.size())
    {
        const std::size_t next = line.find(' ', pos);
        const std::size_t stop = next == std::string::npos ? line.size() : next;
        if (stop > pos) tokens.push_back(line.substr(pos, stop - pos));
        pos = stop + 1;
    }
    if (tokens.empty()) return 0;
    int summary;
    if (tokens[0] == "igpu_launch_timing") summary = 0;
    else if (tokens[0] == "igpu_launch_timing_summary") summary = 1;
    else return 0;

    const char * const msKey = summary ? "max_ms" : "ms";
    const char * const wKey = summary ? "max_w" : "w";
    const char * const hKey = summary ? "max_h" : "h";
    IgpuLbRecord r;
    r.ms = -1.0;
    r.width = 0;
    r.height = 0;
    r.summary = summary;
    for (std::size_t i = 1; i < tokens.size(); ++i)
    {
        const std::size_t eq = tokens[i].find('=');
        if (eq == std::string::npos) continue;
        const std::string key = tokens[i].substr(0, eq);
        const std::string value = tokens[i].substr(eq + 1);
        if (key == "dll") r.dll = value;
        else if (key == "kind") r.kind = value;
        else if (key == "stage") r.stage = value;
        else if (key == msKey)
        {
            double ms = -1.0;
            r.ms = igpu_lb_parse_double(value, &ms) && ms >= 0.0 ? ms : -1.0;
        }
        else if (key == wKey) { if (!igpu_lb_parse_int(value, &r.width)) r.width = 0; }
        else if (key == hKey) { if (!igpu_lb_parse_int(value, &r.height)) r.height = 0; }
    }
    if (r.dll.empty() || r.kind.empty() || r.stage.empty()) return 0;
    *out = r;
    return 1;
}

/* Every record in `text` (one line each; '#' comments and other lines skipped). */
static inline std::vector<IgpuLbRecord> igpu_lb_parse_text(const std::string & text)
{
    std::vector<IgpuLbRecord> records;
    std::size_t pos = 0;
    while (pos <= text.size())
    {
        const std::size_t next = text.find('\n', pos);
        const std::size_t stop = next == std::string::npos ? text.size() : next;
        IgpuLbRecord r;
        if (igpu_lb_parse_line(text.substr(pos, stop - pos), &r)) records.push_back(r);
        if (next == std::string::npos) break;
        pos = next + 1;
    }
    return records;
}

static inline IgpuLbVerdict igpu_lb_verdict(const std::vector<IgpuLbRecord> & records, double budget_ms)
{
    std::map<std::string, IgpuLbStageMax> longest;
    for (const IgpuLbRecord & r : records)
    {
        if (r.ms < 0.0) continue;
        const std::string key = r.dll + '\x1f' + r.kind + '\x1f' + r.stage;
        auto found = longest.find(key);
        if (found == longest.end())
            longest[key] = IgpuLbStageMax{r.dll, r.kind, r.stage, r.ms, r.width, r.height};
        else if (r.ms > found->second.max_ms)
        {
            found->second.max_ms = r.ms;
            found->second.width = r.width;
            found->second.height = r.height;
        }
    }
    IgpuLbVerdict v;
    for (const auto & kv : longest)
    {
        const IgpuLbStageMax & s = kv.second;
        v.stages.push_back(s);
        if (!(s.max_ms > budget_ms)) continue;
        if (s.kind == "kernel") v.violations.push_back(s);
        else if (s.kind == "sync") v.host_waits.push_back(s);
    }
    return v;
}

/* A duration measured at w x h, scaled by pixel count to the largest budgeted
 * frame: the bound for a whole-frame launch, whose grid grows with w*h. 0 for a
 * record with no frame dims. */
static inline double igpu_lb_scaled_to_max_frame_ms(double ms, int width, int height)
{
    if (width <= 0 || height <= 0) return 0.0;
    return ms * ((double)IGPU_LT_BUDGET_MAX_FRAME_W * (double)IGPU_LT_BUDGET_MAX_FRAME_H)
              / ((double)width * (double)height);
}

#endif /* IGPU_LAUNCH_BUDGET_H */
