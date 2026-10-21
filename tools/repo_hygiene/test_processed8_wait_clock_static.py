"""Static guard: the processed8 slice wait reads the wall clock only in its realtime-fallback branch.

PROCESSED8-WAIT-CLOCK-STATIC-GUARD-1 + PROCESSED8-CLOCK-STEP-GUARD-LIVE-AT-HEAD-1 (the #518 r2 fable
key, items 1 and 3): the pipeline clock-step row
(Processed8PrefetchIdleWaitTimesOutWithinMonotonicBudgetWhenWallClockStepsBack) drives a backward
wall-clock step through the test skew seam, so it only sees CLOCK_REALTIME reads that apply that
seam. An unseamed CLOCK_REALTIME abstime reintroduced into mlv_processed8_prefetch_wait_slice_locked
would leave it green. This test reads the source instead: outside the one branch that runs when the
cond fell back to CLOCK_REALTIME (and applies the skew seam there), the slice body must not read the
wall clock at all.

PROCESSED8-COND-CLOCK-INIT-RC-1: mlv_processed8_prefetch_cond_init ignored the return of
pthread_condattr_setclock, so a libc that refused CLOCK_MONOTONIC left the cond on CLOCK_REALTIME
under a MONOTONIC abstime and every slice expired at once. Every pthread_condattr_setclock call in
video_mlv.c must therefore consume its return value.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
VIDEO_MLV_C = ROOT / "src/mlv/video_mlv.c"

SLICE_FUNCTION = "mlv_processed8_prefetch_wait_slice_locked"
SKEW_SEAM = "processed8_prefetch_test_wall_clock_skew_ns"
FALLBACK_GUARD = re.compile(
    r"\bif\s*\(\s*!\s*video\s*->\s*processed8_prefetch_cond_clock_monotonic\s*\)\s*\{")
REALTIME_READ = re.compile(r"\bclock_gettime\s*\(\s*CLOCK_REALTIME\s*,")
WALL_CLOCK_READ = re.compile(
    r"\bCLOCK_REALTIME\w*"
    r"|\bgettimeofday\s*\("
    r"|(?<![\w.>])time\s*\("
    r"|\bGetSystemTime\w*"
    r"|\btimespec_get\s*\("
    r"|\bTIME_UTC\b"
    r"|\b_?ftime\w*\s*\("
    r"|\bsystem_clock\b")
SETCLOCK_CALL = re.compile(r"\bpthread_condattr_setclock\s*\(")
# A call whose value is consumed follows one of these; a statement start (';', '{', '}'), a
# '(void)' cast, an 'if (...)' body or an 'else' discards it.
CONSUMING_TAIL = re.compile(r"(?:[(=!&|,?<>+\-]|\breturn)$")


def _blank_comments_and_literals(text: str) -> str:
    """Same-length copy of text with comments and string/char literals replaced by spaces."""
    out = list(text)
    index = 0
    while index < len(text):
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = len(text) if end < 0 else end + 2
        elif text[index] in "\"'":
            quote = text[index]
            end = index + 1
            while end < len(text) and text[end] != quote:
                end += 2 if text[end] == "\\" else 1
            end = min(end + 1, len(text))
        else:
            index += 1
            continue
        for blank in range(index, end):
            if out[blank] != "\n":
                out[blank] = " "
        index = end
    return "".join(out)


def _matching_brace(text: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unbalanced braces after offset %d" % open_index)


def slice_body_violations(source: str) -> list[str]:
    """Problems with the slice helper's clock reads; empty when the source is guarded."""
    code = _blank_comments_and_literals(source)
    # A definition starts at column 0 with its return type; a call site is indented.
    definitions = list(re.finditer(
        r"(?m)^[A-Za-z_][\w \t*]*\b" + SLICE_FUNCTION + r"\s*\([^;{}]*\)\s*\{", code))
    if len(definitions) != 1:
        return ["expected exactly one definition of %s, found %d" % (SLICE_FUNCTION, len(definitions))]
    body_open = definitions[0].end() - 1
    body = code[body_open:_matching_brace(code, body_open) + 1]

    problems = []
    guards = list(FALLBACK_GUARD.finditer(body))
    if len(guards) != 1:
        problems.append("expected exactly one realtime-fallback branch "
                        "'if (!video->processed8_prefetch_cond_clock_monotonic)', found %d" % len(guards))
        fallback = (0, 0)
    else:
        fallback_open = guards[0].end() - 1
        fallback = (fallback_open, _matching_brace(body, fallback_open) + 1)
        branch = body[fallback[0]:fallback[1]]
        if not REALTIME_READ.search(branch):
            problems.append("the realtime-fallback branch must read CLOCK_REALTIME, the clock its cond measures")
        if SKEW_SEAM not in branch:
            problems.append("the realtime-fallback branch must apply the test wall-clock skew seam " + SKEW_SEAM)
    for read in WALL_CLOCK_READ.finditer(body):
        if not fallback[0] <= read.start() < fallback[1]:
            line = source.count("\n", 0, body_open + read.start()) + 1
            problems.append("wall-clock read %r at video_mlv.c:%d is outside the realtime-fallback branch"
                            % (read.group(0), line))
    return problems


def setclock_violations(source: str) -> list[str]:
    """Every pthread_condattr_setclock call must consume its return value; at least one must exist."""
    code = _blank_comments_and_literals(source)
    calls = list(SETCLOCK_CALL.finditer(code))
    if not calls:
        return ["no pthread_condattr_setclock call found"]
    problems = []
    for call in calls:
        tail = code[:call.start()].rstrip()
        if re.search(r"\(\s*void\s*\)$", tail) or not CONSUMING_TAIL.search(tail):
            line = source.count("\n", 0, call.start()) + 1
            problems.append("pthread_condattr_setclock at video_mlv.c:%d discards its return value" % line)
    return problems


GUARDED_SLICE = """
static int mlv_processed8_prefetch_wait_slice_locked(mlvObject_t * video, double deadline_s)
{
    struct timespec wait_time; /* CLOCK_REALTIME in a comment is not a read */
#if defined(_WIN32) || defined(__APPLE__)
    const int rc = pthread_cond_timedwait_relative_np(&c, &m, &wait_time);
#else
    int64_t abstime_offset_ns = slice_ns;
    if (!video->processed8_prefetch_cond_clock_monotonic)
    {
        if (clock_gettime(CLOCK_REALTIME, &wait_time) != 0)
        {
            return EINVAL;
        }
        abstime_offset_ns += video->processed8_prefetch_test_wall_clock_skew_ns;
    }
    else if (clock_gettime(CLOCK_MONOTONIC, &wait_time) != 0)
    {
        return EINVAL;
    }
    wait_time.tv_sec += (time_t)(abstime_offset_ns / 1000000000LL);
    const int rc = pthread_cond_timedwait(&c, &m, &wait_time);
#endif
    return rc;
}
"""


class Processed8WaitClockStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = VIDEO_MLV_C.read_text(encoding="utf-8", errors="replace")

    def test_slice_body_reads_wall_clock_only_in_realtime_fallback_branch(self) -> None:
        self.assertEqual([], slice_body_violations(self.source))

    def test_every_setclock_return_value_is_consumed(self) -> None:
        self.assertEqual([], setclock_violations(self.source))

    def test_guard_accepts_the_guarded_shape(self) -> None:
        self.assertEqual([], slice_body_violations(GUARDED_SLICE))

    def test_guard_rejects_unseamed_realtime_abstime_outside_fallback(self) -> None:
        mutated = GUARDED_SLICE.replace("else if (clock_gettime(CLOCK_MONOTONIC,",
                                        "else if (clock_gettime(CLOCK_REALTIME,")
        self.assertNotEqual(mutated, GUARDED_SLICE)
        problems = slice_body_violations(mutated)
        self.assertTrue(any("outside the realtime-fallback branch" in p for p in problems), problems)

    def test_guard_rejects_other_wall_clock_reads(self) -> None:
        for read in ("gettimeofday(&tv, NULL);", "now = time(NULL);", "GetSystemTimeAsFileTime(&ft);",
                     "timespec_get(&wait_time, TIME_UTC);"):
            mutated = GUARDED_SLICE.replace("    return rc;\n", "    " + read + "\n    return rc;\n")
            self.assertTrue(slice_body_violations(mutated), read)

    def test_guard_rejects_fallback_without_skew_seam_or_realtime_read(self) -> None:
        unseamed = GUARDED_SLICE.replace(
            "        abstime_offset_ns += video->processed8_prefetch_test_wall_clock_skew_ns;\n", "")
        self.assertTrue(any(SKEW_SEAM in p for p in slice_body_violations(unseamed)))
        monotonic_only = GUARDED_SLICE.replace("clock_gettime(CLOCK_REALTIME,", "clock_gettime(CLOCK_MONOTONIC,")
        self.assertTrue(any("must read CLOCK_REALTIME" in p for p in slice_body_violations(monotonic_only)))

    def test_guard_is_not_vacuous_when_function_or_branch_is_missing(self) -> None:
        renamed = GUARDED_SLICE.replace(SLICE_FUNCTION, SLICE_FUNCTION + "_renamed")
        self.assertTrue(slice_body_violations(renamed))
        unguarded = GUARDED_SLICE.replace("if (!video->processed8_prefetch_cond_clock_monotonic)",
                                          "if (1)")
        self.assertTrue(slice_body_violations(unguarded))

    def test_setclock_guard_rejects_discarded_returns(self) -> None:
        for statement in ("    pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);",
                          "    (void)pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);",
                          "    if (ok) pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);"):
            self.assertTrue(setclock_violations("{\n" + statement + "\n}\n"), statement)
        self.assertTrue(setclock_violations("int x;\n"), "a file with no setclock call must not pass")

    def test_setclock_guard_accepts_consumed_returns(self) -> None:
        for statement in ("    if (pthread_condattr_setclock(&attr, CLOCK_MONOTONIC) == 0) {}",
                          "    rc = pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);",
                          "    ok = ok && pthread_condattr_setclock(&attr, CLOCK_MONOTONIC) == 0;"):
            self.assertEqual([], setclock_violations("{\n" + statement + "\n}\n"), statement)


if __name__ == "__main__":
    unittest.main()
