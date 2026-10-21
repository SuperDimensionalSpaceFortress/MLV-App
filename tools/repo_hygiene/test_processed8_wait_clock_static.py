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
PR #544 r2 keys: the helper may fall back to a default (REALTIME) cond only under 'if (!monotonic)',
and initMlvObject must store that helper's return, and nothing else, in the clock field.
PR #544 r3 keys (sol S1-S3, fable W-08), closed by class rather than by shape: grouping parentheses
are collapsed before the write, address and call checks; no macro may name a clock, a cond call or a
guarded name, or paste tokens; the field assignment must be a live top-level statement; the field
and the helper are each named only where the guard expects them; and the MONOTONIC read must be the
'else if' that follows the realtime-fallback branch, on a literal clock id.
PR #544 r4 key (sol): every check first runs C translation phases 1 and 2 (trigraphs, backslash-newline
splicing) and reads digraphs as their tokens, so a directive or name split across lines, or spelled
'%:define', is checked as the compiler sees it.
"""
import functools
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
COND_INIT_CALL = re.compile(r"\bpthread_cond_init\s*\(")
PLATFORM_DEFAULT_INIT = re.compile(
    r"#[ \t]*if[ \t]+defined\s*\(\s*_WIN32\s*\)\s*\|\|\s*defined\s*\(\s*__APPLE__\s*\)[ \t]*\n"
    r"\s*pthread_cond_init\s*\(\s*\w+\s*,\s*NULL\s*\)\s*;\s*return\s+0\s*;\s*#[ \t]*else\b")
INIT_OBJECT_FUNCTION = "initMlvObject"
CLOCK_FIELD = "processed8_prefetch_cond_clock_monotonic"
# The whole statement, from a statement boundary, so a '(void)' cast or a chained '=' cannot hide in it.
FIELD_ASSIGNMENT = re.compile(
    r"[;{}]\s*(video\s*->\s*" + CLOCK_FIELD + r")\s*=\s*(" + COND_INIT_FUNCTION
    + r")\s*\(\s*&\s*video\s*->\s*processed8_prefetch_cond\s*\)\s*;")
_FIELD_REF = r"(?:\w+\s*(?:->|\.)\s*)*" + CLOCK_FIELD + r"\b"
# A closing ')' between the field and the operator, or an opening '(' after '++', '--' or '&', is a
# parenthesized write or address take (PR #544 r3 sol S1).
FIELD_WRITE = re.compile(
    r"(?:\w+\s*(?:->|\.)\s*)*" + CLOCK_FIELD + r"\s*(?:\)\s*)*(?:(?:[-+*/%&|^]|<<|>>)?=(?!=)|\+\+|--)"
    r"|(?:\+\+|--|(?<!&)&(?!&))[\s(]*" + _FIELD_REF)
DIRECT_PREFETCH_COND_INIT = re.compile(
    r"\bpthread_cond_init\s*\(\s*&\s*(?:\w+\s*(?:->|\.)\s*)*processed8_prefetch_cond\s*,")
GROUPED_CHAIN = re.compile(r"\(\s*[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)*\s*\)")
# A '(' after these keywords groups an expression; after any other identifier, ')' or ']' it opens a
# call or a statement header and is kept.
GROUPING_KEYWORDS = frozenset(("return", "else", "do", "case"))
MACRO_DIRECTIVE = re.compile(r"(?m)^[ \t]*#[ \t]*(?:define|undef)\b(?:\\\r?\n|[^\n])*")
GUARDED_MACRO_TEXT = re.compile(
    r"CLOCK_REALTIME|CLOCK_MONOTONIC|pthread_cond_init|pthread_condattr_setclock|pthread_cond_timedwait"
    r"|clock_gettime|" + COND_INIT_FUNCTION + "|" + SLICE_FUNCTION + r"|processed8_prefetch_cond")
MONOTONIC_READ = re.compile(
    r"\s*else\s+if\s*\(\s*clock_gettime\s*\(\s*CLOCK_MONOTONIC\s*,\s*&\s*wait_time\s*\)")
# C translation phases 1 and 2 (PR #544 r4 sol blocker): trigraphs, which -std=c99 builds enable, and
# backslash-newline splices, which GCC also takes across trailing blanks.
TRIGRAPH = re.compile(r"\?\?([=(/)'<!>-])")
TRIGRAPHS = {"=": "#", "(": "[", "/": "\\", ")": "]", "'": "^", "<": "{", "!": "|", ">": "}", "-": "~"}
SPLICED_LINE_END = re.compile(r"\\[ \t\f\v]*\r?\Z")
# Digraph tokens, longest first, mapped to same-length spellings of the tokens they stand for.
DIGRAPHS = {"%:%:": "##  ", "%:": "# ", "<:": "[ ", ":>": " ]", "<%": "{ ", "%>": " }"}
DIGRAPH = re.compile("|".join(re.escape(digraph) for digraph in DIGRAPHS))


@functools.lru_cache(maxsize=4)
def _translated(source: str) -> str:
    """source after C translation phases 1 and 2, so a directive or identifier split across lines is
    seen whole, as the compiler sees it. The newlines a logical line absorbed are re-emitted after it,
    so every later line keeps its number. Every check translates its raw source exactly once."""
    lines = TRIGRAPH.sub(lambda match: TRIGRAPHS[match.group(1)], source).split("\n")
    pieces, absorbed = [], 0
    for index, line in enumerate(lines):
        spliced = SPLICED_LINE_END.sub("", line) if index < len(lines) - 1 else line
        if spliced != line:
            pieces.append(spliced)
            absorbed += 1
        else:
            pieces.append(line + "\n" * (absorbed + (index < len(lines) - 1)))
            absorbed = 0
    return "".join(pieces)


@functools.lru_cache(maxsize=4)  # each check re-blanks the same ~0.5 MB source
def _blank_comments_and_literals(text: str) -> str:
    """Same-length copy of translated text with comments and string/char literals replaced by spaces
    and each digraph replaced by the token it spells."""
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
    return DIGRAPH.sub(lambda match: DIGRAPHS[match.group(0)], "".join(out))


@functools.lru_cache(maxsize=4)
def _collapse_grouping_parens(code: str) -> str:
    """Same-length copy of blanked code with every grouping '( <identifier or member chain> )' reduced
    to the bare chain, so '(video->field) = 1' reads as 'video->field = 1' and '(helper)(x)' as
    'helper (x)'. Call and statement-header parentheses are kept."""
    while True:
        out = list(code)
        for group in GROUPED_CHAIN.finditer(code):
            before = group.start() - 1
            while before >= 0 and code[before].isspace():
                before -= 1
            word_end = before + 1
            while before >= 0 and (code[before].isalnum() or code[before] == "_"):
                before -= 1
            word = code[before + 1:word_end]
            if (word and word not in GROUPING_KEYWORDS) or (not word and before >= 0 and code[before] in ")]"):
                continue
            out[group.start()] = out[group.end() - 1] = " "
        collapsed = "".join(out)
        if collapsed == code:
            return collapsed
        code = collapsed


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
    source = _translated(source)
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
    # PR #544 r3 sol S3 and fable W-08: a macro or numeric clock id hides a wall-clock read from the
    # scan above, and a missing 'else' runs the MONOTONIC read after the REALTIME one.
    grouped = _collapse_grouping_parens(code)[body_open:body_open + len(body)]
    gettimes = list(re.finditer(r"\bclock_gettime\b", grouped))
    if len(gettimes) != 2:
        problems.append("the slice must name clock_gettime exactly twice (the REALTIME fallback read and "
                        "the MONOTONIC read), found %d" % len(gettimes))
    for read in gettimes:
        call = re.match(r"clock_gettime\s*\(([^(),]*),", grouped[read.start():])
        clock = call.group(1).strip() if call else ""
        if clock not in ("CLOCK_REALTIME", "CLOCK_MONOTONIC"):
            line = source.count("\n", 0, body_open + read.start()) + 1
            problems.append("clock_gettime at video_mlv.c:%d must name CLOCK_REALTIME or CLOCK_MONOTONIC "
                            "literally, found %r" % (line, clock))
    if len(guards) == 1 and not MONOTONIC_READ.match(body, fallback[1]):
        problems.append("the realtime-fallback branch must be followed directly by "
                        "'else if (clock_gettime(CLOCK_MONOTONIC, &wait_time)'")
    return problems


def setclock_violations(source: str) -> list[str]:
    """Every pthread_condattr_setclock call must consume its return value; at least one must exist."""
    source = _translated(source)
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
    source = _translated(source)
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


def _function_body(code: str, name: str) -> tuple[int, str, str]:
    """(open-brace offset, body, problem) of the one column-0 definition of name in blanked code."""
    definitions = list(re.finditer(r"(?m)^[A-Za-z_][\w \t*]*\b" + name + r"\s*\([^;{}]*\)\s*\{", code))
    if len(definitions) != 1:
        return -1, "", "expected exactly one definition of %s, found %d" % (name, len(definitions))
    body_open = definitions[0].end() - 1
    return body_open, code[body_open:_matching_brace(code, body_open) + 1], ""


def cond_reinit_violations(source: str) -> list[str]:
    """PR #544 r2 key3 K-01: inside the cond-init helper a default-attr (CLOCK_REALTIME) cond init
    may run only in the platform branch that returns 0, or as the sole statement of the top-level
    'if (!monotonic)' re-init that runs after the attr init and right before 'return monotonic;'."""
    source = _translated(source)
    code = _blank_comments_and_literals(source)
    body_open, body, problem = _function_body(code, COND_INIT_FUNCTION)
    if problem:
        return [problem]

    def line(offset: int) -> int:
        return source.count("\n", 0, body_open + offset) + 1

    problems = []
    if len(re.findall(r"(?m)^[ \t]*#[ \t]*if", body)) != 1:
        problems.append("%s must have exactly one preprocessor conditional, the platform split"
                        % COND_INIT_FUNCTION)
    platform = list(PLATFORM_DEFAULT_INIT.finditer(body))
    if len(platform) != 1:
        problems.append("%s must open with '#if defined(_WIN32) || defined(__APPLE__)' "
                        "pthread_cond_init(<cond>, NULL); return 0; '#else', found %d" % (COND_INIT_FUNCTION, len(platform)))
    allowed = [(match.start(), match.end()) for match in platform]
    attr_inits, default_inits = [], []
    for call in COND_INIT_CALL.finditer(body):
        args = re.sub(r"\s+", "", body[call.end():_matching_paren(body, call.end() - 1)]).split(",")
        if len(args) == 2 and args[1].startswith("&"):
            attr_inits.append((call, args[0]))
        elif len(args) == 2 and args[1] == "NULL":
            default_inits.append((call, args[0]))
        else:
            problems.append("unrecognized pthread_cond_init at video_mlv.c:%d" % line(call.start()))
    reinits = []
    for branch in re.finditer(r"\bif\s*\(", body):
        close = _matching_paren(body, branch.end() - 1)
        if re.sub(r"\s+", "", body[branch.end():close]) == "!monotonic":
            reinits.append((branch, close))
    if len(reinits) != 1 or len(attr_inits) != 1:
        problems.append("expected exactly one 'if (!monotonic)' re-init after exactly one attr cond init "
                        "in %s, found %d and %d" % (COND_INIT_FUNCTION, len(reinits), len(attr_inits)))
    else:
        (branch, close), (attr_call, cond) = reinits[0], attr_inits[0]
        brace = re.match(r"\s*\{", body[close + 1:])
        depth = body[:branch.start()].count("{") - body[:branch.start()].count("}")
        if not brace or depth != 1 or branch.start() < attr_call.start():
            problems.append("the re-init 'if (!monotonic)' at video_mlv.c:%d must be a braced top-level "
                            "branch after the attr cond init" % line(branch.start()))
        else:
            reinit_open = close + 1 + brace.end() - 1
            reinit_close = _matching_brace(body, reinit_open)
            if re.sub(r"\s+", "", body[reinit_open + 1:reinit_close]) != "pthread_cond_init(%s,NULL);" % cond:
                problems.append("the re-init branch at video_mlv.c:%d must hold only "
                                "'pthread_cond_init(%s, NULL);'" % (line(branch.start()), cond))
            else:
                allowed.append((reinit_open, reinit_close))
            if not re.match(r"\s*return\s+monotonic\s*;\s*#[ \t]*endif\s*\}\s*\Z", body[reinit_close + 1:]):
                problems.append("the re-init branch at video_mlv.c:%d must be followed only by "
                                "'return monotonic;' and '#endif'" % line(branch.start()))
    if platform and len(re.findall(r"\breturn\b", body[platform[0].end():])) != 1:
        problems.append("the POSIX branch of %s must return exactly once, after the re-init" % COND_INIT_FUNCTION)
    for call, _ in default_inits:
        if not any(start <= call.start() < end for start, end in allowed):
            problems.append("default-attr pthread_cond_init at video_mlv.c:%d is outside the platform "
                            "branch and the 'if (!monotonic)' re-init" % line(call.start()))
    return problems


def field_wiring_violations(source: str) -> list[str]:
    """PR #544 r2 key3 K-02 and sol mutations A/B: initMlvObject must store the guarded helper's
    return for &video->processed8_prefetch_cond in the clock field, once, and nothing else may write
    that field, call the helper or init that cond."""
    source = _translated(source)
    code = _blank_comments_and_literals(source)
    body_open, body, problem = _function_body(code, INIT_OBJECT_FUNCTION)
    if problem:
        return [problem]
    problems = []
    assignments = list(FIELD_ASSIGNMENT.finditer(body))
    if len(assignments) != 1:
        problems.append("%s must run 'video->%s = %s(&video->processed8_prefetch_cond);' exactly once, found %d"
                        % (INIT_OBJECT_FUNCTION, CLOCK_FIELD, COND_INIT_FUNCTION, len(assignments)))
    expected = [body_open + assignments[0].start(1)] if len(assignments) == 1 else []
    # PR #544 r3 sol S1/S2: '(video->field) = 1' and '(helper)(...)' are checked as their bare forms.
    grouped = _collapse_grouping_parens(code)
    for write in FIELD_WRITE.finditer(grouped):
        if write.start() not in expected:
            problems.append("%s is written or aliased at video_mlv.c:%d outside the one helper assignment"
                            % (CLOCK_FIELD, source.count("\n", 0, write.start()) + 1))
    calls = len(re.findall(r"\b" + COND_INIT_FUNCTION + r"\s*\(", grouped)) - 1
    if calls != 1:
        problems.append("%s must be called exactly once (the field assignment), found %d" % (COND_INIT_FUNCTION, calls))
    for direct in DIRECT_PREFETCH_COND_INIT.finditer(grouped):
        problems.append("processed8_prefetch_cond is initialised at video_mlv.c:%d without %s"
                        % (source.count("\n", 0, direct.start()) + 1, COND_INIT_FUNCTION))
    if len(assignments) == 1:
        # PR #544 r3 sol S2: the assignment must run, so it is a top-level statement of initMlvObject
        # that no 'if (0)', '#if 0', goto or earlier return can skip.
        start = assignments[0].start(1)
        before = body[:start]
        line = source.count("\n", 0, body_open + start) + 1
        if before.count("{") - before.count("}") != 1 or before.count("(") != before.count(")"):
            problems.append("the %s assignment at video_mlv.c:%d must be a top-level statement of %s"
                            % (CLOCK_FIELD, line, INIT_OBJECT_FUNCTION))
        if re.search(r"\bgoto\b", body) or re.search(r"\breturn\b", before):
            problems.append("the %s assignment at video_mlv.c:%d must not be skippable by a goto or an "
                            "earlier return" % (CLOCK_FIELD, line))
        conditionals = 0
        for directive in re.finditer(r"(?m)^[ \t]*#[ \t]*(if|ifdef|ifndef|endif)\b", code[:body_open + start]):
            conditionals += -1 if directive.group(1) == "endif" else 1
        if conditionals:
            problems.append("the %s assignment at video_mlv.c:%d sits inside a preprocessor conditional"
                            % (CLOCK_FIELD, line))
        # The field is named only by this assignment and the slice's fallback guard, and the helper
        # only by its definition and this assignment.
        allowed = {body_open + start + assignments[0].group(1).index(CLOCK_FIELD)}
        slice_open, slice_body, _ = _function_body(code, SLICE_FUNCTION)
        for guard in FALLBACK_GUARD.finditer(slice_body):
            allowed.add(slice_open + guard.start() + guard.group(0).index(CLOCK_FIELD))
        for ref in re.finditer(r"\b" + CLOCK_FIELD + r"\b", code):
            if ref.start() not in allowed:
                problems.append("%s is named at video_mlv.c:%d outside the %s assignment and the slice's "
                                "fallback guard" % (CLOCK_FIELD, source.count("\n", 0, ref.start()) + 1,
                                                    INIT_OBJECT_FUNCTION))
        helper_refs = [ref.start() for ref in re.finditer(r"\b" + COND_INIT_FUNCTION + r"\b", code)]
        if len(helper_refs) != 2 or body_open + assignments[0].start(2) not in helper_refs:
            problems.append("%s must be named exactly twice (its definition and the %s assignment), found %d"
                            % (COND_INIT_FUNCTION, INIT_OBJECT_FUNCTION, len(helper_refs)))
    return problems


def macro_violations(source: str) -> list[str]:
    """PR #544 r3 sol S3: no '#define' or '#undef' in video_mlv.c may name a clock, a cond or clock
    call, or a guarded name, and none may paste tokens, so the text checks see what the compiler sees."""
    source = _translated(source)
    code = _blank_comments_and_literals(source)
    problems = []
    for directive in MACRO_DIRECTIVE.finditer(code):
        text = directive.group(0)
        if GUARDED_MACRO_TEXT.search(text) or "##" in text:
            problems.append("macro at video_mlv.c:%d renames a guarded clock, call or name, or pastes tokens: %s"
                            % (source.count("\n", 0, directive.start()) + 1, " ".join(text.split())))
    return problems


def _all_violations(source: str) -> list[str]:
    return (slice_body_violations(source) + setclock_violations(source) + cond_init_violations(source)
            + cond_reinit_violations(source) + field_wiring_violations(source) + macro_violations(source))


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

    def test_fallback_reinit_and_field_wiring_are_pinned(self) -> None:
        self.assertEqual([], cond_reinit_violations(self.source))
        self.assertEqual([], field_wiring_violations(self.source))

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

    def test_guard_rejects_fallback_reinit_and_field_wiring_slips_in_head_source(self) -> None:
        # PR #544 r2 keys (key3 opus K-01/K-02, sol mutations A/B): each slip leaves the cond on one
        # clock and the field naming the other. Each mutation is applied to the real head source.
        reinit = "    if (!monotonic)\n    {\n        pthread_cond_init(cond, NULL);\n    }\n"
        assignment = ("    video->processed8_prefetch_cond_clock_monotonic =\n"
                      "        mlv_processed8_prefetch_cond_init(&video->processed8_prefetch_cond);\n")
        init_object = "mlvObject_t * initMlvObject()\n"
        mutations = {
            "fallback re-init on monotonic": (reinit, reinit.replace("if (!monotonic)", "if (monotonic)")),
            "fallback re-init unconditional": (reinit, "    pthread_cond_init(cond, NULL);\n"),
            "field assignment deleted": (
                assignment, "    mlv_processed8_prefetch_cond_init(&video->processed8_prefetch_cond);\n"),
            "field forced to 1, helper result ignored": (
                assignment, "    video->processed8_prefetch_cond_clock_monotonic = 1;\n"
                            "    (void)mlv_processed8_prefetch_cond_init(&video->processed8_prefetch_cond);\n"),
            "field from a default-attr helper": (
                assignment + "|" + init_object,
                assignment.replace("mlv_processed8_prefetch_cond_init(", "mlv_processed8_prefetch_cond_init_default(")
                + "|static int mlv_processed8_prefetch_cond_init_default(pthread_cond_t * cond)\n"
                  "{\n    pthread_cond_init(cond, NULL);\n    return 1;\n}\n\n" + init_object),
        }
        for name, (old, new) in mutations.items():
            with self.subTest(mutation=name):
                mutated = self.source
                for old_part, new_part in zip(old.split("|"), new.split("|")):
                    self.assertEqual(1, mutated.count(old_part), name)
                    mutated = mutated.replace(old_part, new_part)
                self.assertTrue(_all_violations(mutated), name)

    def test_no_macro_renames_a_guarded_clock_call_or_name(self) -> None:
        self.assertEqual([], macro_violations(self.source))

    def test_guard_rejects_r3_text_bypass_classes_in_head_source(self) -> None:
        # PR #544 r3 keys (sol S1-S3, fable W-08): each edit leaves the code compiling and the cond and
        # the field on different clocks, or the MONOTONIC slice reading the wall clock. Each mutation
        # is applied to the real head source.
        assignment = ("    video->processed8_prefetch_cond_clock_monotonic =\n"
                      "        mlv_processed8_prefetch_cond_init(&video->processed8_prefetch_cond);\n")
        init_object = "mlvObject_t * initMlvObject()\n"
        slice_definition = "static int %s(mlvObject_t * video,\n" % SLICE_FUNCTION
        monotonic_read = "    else if (clock_gettime(CLOCK_MONOTONIC, &wait_time) != 0)\n"
        mutations = {
            "S1 parenthesized field write": [
                (assignment, assignment + "    (video->%s) = 1;\n" % CLOCK_FIELD)],
            "S1 parenthesized prefix increment": [
                (assignment, assignment + "    ++(video->%s);\n" % CLOCK_FIELD)],
            "S1 parenthesized postfix increment": [
                (assignment, assignment + "    (video->%s)++;\n" % CLOCK_FIELD)],
            "S1 parenthesized address take": [
                (assignment, assignment + "    int * clock_flag = &(video->%s);\n    *clock_flag = 1;\n" % CLOCK_FIELD)],
            "S2 dead assignment, live parenthesized helper call": [
                (assignment, "    if (0)\n    {\n" + assignment + "    }\n"
                             "    (%s)(&video->processed8_prefetch_cond);\n" % COND_INIT_FUNCTION)],
            "S2 assignment skipped by goto": [
                (assignment, "    goto skip_clock_field;\n" + assignment + "skip_clock_field:\n    ;\n")],
            "S3 macro-aliased CLOCK_REALTIME in the MONOTONIC read": [
                (slice_definition, "#define PROCESSED8_WAIT_CLOCK CLOCK_REALTIME\n" + slice_definition),
                (monotonic_read, monotonic_read.replace("CLOCK_MONOTONIC", "PROCESSED8_WAIT_CLOCK"))],
            "S3 object-like macro redirects the helper": [
                (init_object, "static int %s_default(pthread_cond_t * cond)\n"
                              "{\n    pthread_cond_init(cond, NULL);\n    return 1;\n}\n"
                              "#define %s %s_default\n\n" % ((COND_INIT_FUNCTION,) * 3) + init_object)],
            "S3 numeric clock id in the MONOTONIC read": [
                (monotonic_read, monotonic_read.replace("CLOCK_MONOTONIC", "0"))],
            "S3 token-pasted field write": [
                (init_object, "#define MLV_PASTE(a, b) a##b\n\n" + init_object),
                (assignment, assignment + "    video->MLV_PASTE(processed8_prefetch_cond_clock, _monotonic) = 1;\n")],
            "W-08 else deleted before the MONOTONIC read": [
                (monotonic_read, monotonic_read.replace("    else if", "    if"))],
        }
        for name, edits in mutations.items():
            with self.subTest(mutation=name):
                mutated = self.source
                for old, new in edits:
                    self.assertEqual(1, mutated.count(old), name)
                    mutated = mutated.replace(old, new)
                self.assertTrue(_all_violations(mutated), name)

    def test_guard_rejects_lexer_phase_evasions_in_head_source(self) -> None:
        # PR #544 r4 sol blocker and fable "line-splice/digraph lexer evasion": the compiler splices
        # backslash-newlines and reads '%:' as '#' before it sees a directive or an identifier, so
        # each edit below compiles into a macro alias or a wall-clock read that the text checks must
        # still find. Each mutation is applied to the real head source.
        cond_init_definition = "static int %s(pthread_cond_t * cond)\n" % COND_INIT_FUNCTION
        slice_definition = "static int %s(mlvObject_t * video,\n" % SLICE_FUNCTION
        after_monotonic_read = "    wait_time.tv_nsec += (long)(abstime_offset_ns % 1000000000LL);\n"
        mutations = {
            "L-01 spliced setclock macro reports success": [
                (cond_init_definition, "#define pthread_condattr_set\\\nclock(attr, clk) (0)\n" + cond_init_definition)],
            "L-02 spliced realtime read after the MONOTONIC read": [
                (after_monotonic_read, "    clock_gett\\\nime(CLOCK_REAL\\\nTIME, &wait_time);\n" + after_monotonic_read)],
            "L-03 spliced directives alias CLOCK_MONOTONIC to CLOCK_REALTIME": [
                (slice_definition, "#un\\\ndef CLOCK_MONOTONIC\n#def\\\nine CLOCK_MONOTONIC CLOCK_REALTIME\n"
                                   + slice_definition)],
            "L-04 digraph directive reroutes the slice clock": [
                (slice_definition, "%:define PROCESSED8_WAIT_CLOCK CLOCK_REALTIME\n"
                                   "%:define clock_gettime(clock, ts) clock_gettime(PROCESSED8_WAIT_CLOCK, ts)\n"
                                   + slice_definition)],
        }
        for name, edits in mutations.items():
            with self.subTest(mutation=name):
                mutated = self.source
                for old, new in edits:
                    self.assertEqual(1, mutated.count(old), name)
                    mutated = mutated.replace(old, new)
                self.assertTrue(_all_violations(mutated), name)

    def test_translation_phases_match_the_compiler(self) -> None:
        # Phase 1 folds trigraphs (-std=c99 builds enable them), phase 2 deletes every backslash-newline
        # (GCC also splices across trailing blanks), and the lines a splice absorbed are re-emitted so
        # later line numbers hold. Digraphs read as their tokens outside comments and literals.
        self.assertEqual("#define ab x\n\n\nint y;\n", _translated("??=define a\\\nb \\  \nx\nint y;\n"))
        code = _blank_comments_and_literals('%:define M <%a<:0:>%> %:%: "%:" /* <% */\n')
        self.assertEqual("# define M { a[ 0 ] } ##  ", code[:26])
        self.assertEqual("", code[26:].strip())


if __name__ == "__main__":
    unittest.main()
