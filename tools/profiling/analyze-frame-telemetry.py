"""Round-2 item 4: stall-tail stage attribution from playback_smoke.frame lines.

Parses per-frame telemetry from a trace log (latest session only is the
caller's responsibility - pass per-run logs), computes p50/p90/p99 per field,
and attributes the slowest-decile frames' render time to stages.

A non-numeric cell (the app prints `na` for a stage the frame's render did not run)
is UNMEASURED, never zero: percentiles, means and the stage split use measured rows
only, per field; a field with no measured row prints `na`; and when the log has any
`na` cell every line names its measured row count. A log with no `na` cell prints
exactly what it printed before.
"""
import re
import sys
from collections import defaultdict

FIELDS = [
    "interval_ms", "render_total_ms", "render_work_ms", "queue_wait_ms",
    "llrawproc_ms", "processed8_ms", "draw_total_ms",
    "present_ui_signal_latency_ms", "present_draw_present_ms",
    "present_overlays_scopes_ms", "present_render_slot_release_ms",
    "present_pacing_ms",
    "processed16_setup_ms", "processed16_core_math_ms",
    "processed16_local_tone_ms", "processed16_threading_overhead_ms",
    "processed8_setup_ms", "processed8_core_math_ms",
    "processed8_local_tone_ms", "processed8_threading_overhead_ms",
]
RX = re.compile(r"playback_smoke\.frame .*")


def percentile(sv, p):
    if not sv:
        return 0.0
    k = (len(sv) - 1) * p
    f = int(k)
    c = min(f + 1, len(sv) - 1)
    return sv[f] if f == c else sv[f] + (sv[c] - sv[f]) * (k - f)


def measured(rows, f):
    # An absent key keeps the historical 0.0; an `na` cell (None) is left out.
    return [v for v in (r.get(f, 0.0) for r in rows) if v is not None]


def analyze(path):
    frames = []
    has_na = False
    with open(path, errors="replace") as fh:
        for line in fh:
            m = RX.search(line)
            if not m:
                continue
            row = {}
            for kv in m.group(0).split():
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    try:
                        row[k] = float(v)
                    except ValueError:
                        row.setdefault(k, None)
            if row.get("render_total_ms") is not None:
                frames.append(row)
                has_na = has_na or any(row.get(f, 0.0) is None for f in FIELDS)

    def count(k, n):
        return f"  (measured {k}/{n})" if has_na else ""

    print(f"\n=== {path}  (frames={len(frames)}) ===")
    print(f"{'field':<36}{'p50':>8}{'p90':>8}{'p99':>8}{'max':>8}")
    for f in FIELDS:
        vals = sorted(measured(frames, f))
        if frames and not vals:
            print(f"{f:<36}{'na':>8}{'na':>8}{'na':>8}{'na':>8}{count(0, len(frames))}")
            continue
        if not vals or vals[-1] == 0.0:
            continue
        print(f"{f:<36}{percentile(vals, .5):>8.1f}{percentile(vals, .9):>8.1f}"
              f"{percentile(vals, .99):>8.1f}{vals[-1]:>8.1f}{count(len(vals), len(frames))}")

    # slowest decile by render_total: average stage split inside those frames
    frames.sort(key=lambda r: r.get("render_total_ms", 0.0))
    slow = frames[int(len(frames) * 0.9):]
    if slow:
        n = len(slow)

        def avg(f, rows=slow):
            vals = measured(rows, f)
            return sum(vals) / len(vals) if vals else None

        def share(v):
            return f"{v:6.1f} ms  {100 * v / rt:5.1f}%" if v is not None else f"{'na':>6}"

        def ms(v):
            return f"{v:6.1f} ms" if v is not None else f"{'na':>6}"

        rt = avg("render_total_ms") or 1.0
        # decode is the remainder of render work after llrawproc+processed8, taken only
        # over the decile rows where all three terms are measured
        terms = ("render_work_ms", "llrawproc_ms", "processed8_ms")
        joint = [r for r in slow if all(r.get(f, 0.0) is not None for f in terms)]
        decode = (max(avg(terms[0], joint) - avg(terms[1], joint) - avg(terms[2], joint), 0.0)
                  if joint else None)
        intervals = measured(slow, "interval_ms")
        print(f"slowest-decile (n={n}) render_total avg {rt:.1f} ms split:")
        print(f"  decode+debayer+scale (residual) {share(decode)}{count(len(joint), n)}")
        print(f"  llrawproc (recon etc.)          {share(avg('llrawproc_ms'))}"
              f"{count(len(measured(slow, 'llrawproc_ms')), n)}")
        print(f"  processed8 (processing+pack)    {share(avg('processed8_ms'))}"
              f"{count(len(measured(slow, 'processed8_ms')), n)}")
        print(f"  queue_wait                      {ms(avg('queue_wait_ms'))}"
              f"{count(len(measured(slow, 'queue_wait_ms')), n)}")
        print(f"  draw_total (present side)       {ms(avg('draw_total_ms'))}"
              f"{count(len(measured(slow, 'draw_total_ms')), n)}")
        print(f"  worst interval in decile        {ms(max(intervals) if intervals else None)}"
              f"{count(len(intervals), n)}")


for p in sys.argv[1:]:
    analyze(p)
