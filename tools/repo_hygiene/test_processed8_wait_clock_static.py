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
video_mlv.c must therefore consume its return value, and (PR #544 sol r1) the init helper may set
its monotonic flag only in the branch where setclock and the cond init both returned exactly 0.
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
COND_INIT_FUNCTION = "mlv_processed8_prefetch_cond_init"
# Whitespace-stripped condition of the only branch allowed to set monotonic = 1.
EXACT_SUCCESS = re.compile(
    r"pthread_condattr_setclock\((&\w+),CLOCK_MONOTONIC\)==0"
    r"&&pthread_cond_init\([^(),]+,\1\)==0\Z")
ATTR_COND_INIT = re.compile(r"\bpthread_cond_init\s*\(\s*[^(),]+,\s*&")
FLAG_WRITE = re.compile(
    r"\bmonotonic\s*(?:[-+*/%&|^]|<<|>>)?=(?!=)|(?:\+\+|--)\s*monotonic\b|\bmonotonic\s*(?:\+\+|--)")


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


def _matching_paren(text: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("unbalanced parentheses after offset %d" % open_index)


def cond_init_violations(source: str) -> list[str]:
    """The cond-init helper may report CLOCK_MONOTONIC only when setclock and the cond init both
    returned exactly 0 (PR #544 sol r1: '>= 0' takes a positive EINVAL as success)."""
    code = _blank_comments_and_literals(source)
    definitions = list(re.finditer(
        r"(?m)^[A-Za-z_][\w \t*]*\b" + COND_INIT_FUNCTION + r"\s*\([^;{}]*\)\s*\{", code))
    if len(definitions) != 1:
        return ["expected exactly one definition of %s, found %d" % (COND_INIT_FUNCTION, len(definitions))]
    body_open = definitions[0].end() - 1
    body = code[body_open:_matching_brace(code, body_open) + 1]

    def line(offset: int) -> int:
        return source.count("\n", 0, body_open + offset) + 1

    problems = []
    declaration = re.search(r"\bint\s+monotonic\s*=\s*0\s*;", body)
    if not declaration:
        problems.append("%s must start its flag as 'int monotonic = 0;'" % COND_INIT_FUNCTION)
    success = None
    setclock_branches = []
    for branch in re.finditer(r"\bif\s*\(", body):
        close = _matching_paren(body, branch.end() - 1)
        if SETCLOCK_CALL.search(body[branch.end():close]):
            setclock_branches.append((branch, close))
    if len(setclock_branches) != 1:
        problems.append("expected exactly one 'if' testing pthread_condattr_setclock in %s, found %d"
                        % (COND_INIT_FUNCTION, len(setclock_branches)))
    else:
        branch, close = setclock_branches[0]
        condition = re.sub(r"\s+", "", body[branch.end():close])
        brace = re.match(r"\s*\{", body[close + 1:])
        if not EXACT_SUCCESS.match(condition):
            problems.append("the setclock branch at video_mlv.c:%d must be exactly "
                            "'pthread_condattr_setclock(<attr>, CLOCK_MONOTONIC) == 0 && "
                            "pthread_cond_init(<cond>, <attr>) == 0'" % line(branch.start()))
        elif not brace:
            problems.append("the setclock branch at video_mlv.c:%d needs a braced body" % line(branch.start()))
        else:
            success_open = close + 1 + brace.end() - 1
            success = (success_open, _matching_brace(body, success_open) + 1)
    if len(SETCLOCK_CALL.findall(body)) != 1:
        problems.append("%s must call pthread_condattr_setclock exactly once" % COND_INIT_FUNCTION)
    if len(ATTR_COND_INIT.findall(body)) != 1:
        problems.append("%s must init the cond from the attr exactly once, inside the setclock branch"
                        % COND_INIT_FUNCTION)
    sets = []
    for write in FLAG_WRITE.finditer(body):
        if declaration and write.start() == declaration.start() + declaration.group(0).index("monotonic"):
            continue
        inside = success is not None and success[0] <= write.start() < success[1]
        if not inside or not re.match(r"monotonic\s*=\s*1\s*;", body[write.start():]):
            problems.append("monotonic is written at video_mlv.c:%d outside 'monotonic = 1;' in the "
                            "exact-success branch" % line(write.start()))
        sets.append(write)
    if not sets:
        problems.append("%s never sets monotonic = 1" % COND_INIT_FUNCTION)
    for ret in re.finditer(r"\breturn\b([^;]*);", body):
        if ret.group(1).strip() not in ("0", "monotonic"):
            problems.append("%s may return only 0 or monotonic; video_mlv.c:%d returns %r"
                            % (COND_INIT_FUNCTION, line(ret.start()), ret.group(1).strip()))
    return problems


def _all_violations(source: str) -> list[str]:
    return slice_body_violations(source) + setclock_violations(source) + cond_init_violations(source)


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

    def test_guard_rejects_inexact_cond_init_success_checks_in_head_source(self) -> None:
        # PR #544 sol r1 blocker: '>= 0' accepts a positive EINVAL as success, so the default
        # REALTIME cond is reported as MONOTONIC. Each mutation is applied to the real head source.
        setclock = "pthread_condattr_setclock(&attr, CLOCK_MONOTONIC)"
        cond_init = "pthread_cond_init(cond, &attr)"
        success = "if (%s == 0\n            && %s == 0)\n        {\n            monotonic = 1;\n        }\n" % (
            setclock, cond_init)
        mutations = {
            "setclock >= 0": (setclock + " == 0", setclock + " >= 0"),
            "setclock != -1": (setclock + " == 0", setclock + " != -1"),
            "setclock bare truthiness": (setclock + " == 0", setclock),
            "cond_init >= 0": (cond_init + " == 0", cond_init + " >= 0"),
            "either call succeeding": ("\n            && " + cond_init, "\n            || " + cond_init),
            "setclock rc ignored": (success, "int rc = %s;\n        %s;\n        monotonic = 1;\n" % (
                setclock, cond_init)),
        }
        for name, (old, new) in mutations.items():
            with self.subTest(mutation=name):
                self.assertEqual(1, self.source.count(old), name)
                self.assertTrue(_all_violations(self.source.replace(old, new)), name)

    def test_cond_init_reports_monotonic_only_after_exact_success(self) -> None:
        self.assertEqual([], cond_init_violations(self.source))

    def test_cond_init_guard_rejects_other_monotonic_escapes_in_head_source(self) -> None:
        mutations = {
            "return 1": ("    return monotonic;\n#endif", "    return 1;\n#endif"),
            "flag set after the branch": (
                "        pthread_condattr_destroy(&attr);\n    }\n    if (!monotonic)",
                "        pthread_condattr_destroy(&attr);\n        monotonic = 1;\n    }\n    if (!monotonic)"),
            "helper renamed": ("static int %s(" % COND_INIT_FUNCTION, "static int %s_renamed(" % COND_INIT_FUNCTION),
        }
        for name, (old, new) in mutations.items():
            with self.subTest(mutation=name):
                self.assertEqual(1, self.source.count(old), name)
                self.assertTrue(cond_init_violations(self.source.replace(old, new)), name)

    def test_setclock_guard_accepts_consumed_returns(self) -> None:
        for statement in ("    if (pthread_condattr_setclock(&attr, CLOCK_MONOTONIC) == 0) {}",
                          "    rc = pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);",
                          "    ok = ok && pthread_condattr_setclock(&attr, CLOCK_MONOTONIC) == 0;"):
            self.assertEqual([], setclock_violations("{\n" + statement + "\n}\n"), statement)


if __name__ == "__main__":
    unittest.main()
