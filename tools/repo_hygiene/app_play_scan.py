"""PLAYBACK-CLIP-LENGTH-ENFORCE-2 round 2: the static class scan over the app's C++ for programmatic Play.

A pure function over ``{path: text}`` so a test can MUTATE the real sources in memory and require the scan to go
red (a scan that passes on a deliberately broken tree is worthless).

What changed from round 1 (sol hardening): the allowlist is no longer "any line carrying an ``allowlisted:``
comment". The scan strips comments and string literals with a real tokenizer (a ``//`` inside a string cannot hide
code, a block comment cannot carry an exemption), matches over the WHOLE file (a line break between
``actionPlay`` and ``->trigger`` cannot split a match), and treats EVERY use of the Play / Loop action as
unreviewed unless it is one of a short list of safe forms or sits inside one of three PINNED helper bodies whose
exact text is compared with a pinned string. Adding an exemption therefore means editing this file, in review.

Violations are returned as ``path:line: message`` strings; an empty list means the class is closed.
"""

from __future__ import annotations

import re

PLAY_SOURCES = ("platform/qt/MainWindow.cpp", "platform/qt/main.cpp")

# Every programmaticPlay( call site, with how many times it may appear and what the REQUESTED window argument
# must start with. A NEW site, a duplicated site, or a changed request expression fails the scan.
REVIEWED_PROGRAMMATIC_PLAY_SITES = {
    "autoplay": (1, "autoplaySeconds"),                                          # MLVAPP_AUTOPLAY_* hook (normal GUI)
    "profile-look-assist-settle": (1, "playback_frame_range::kMinPlayWindowSeconds"),
    "profile-exercise-play-action": (1, "playback_frame_range::kMinPlayWindowSeconds"),
    "gui-smoke-measured": (1, "playback_frame_range::smokePlayRequestSeconds("),
}

# Every programmaticStop( call site (an automation mode that ENDS a Play on its own clock) and how many times.
REVIEWED_PROGRAMMATIC_STOP_SITES = {
    "profile-look-assist-settle": 1,       # after the 20 s hold (an early end is detected after it, as a typed failure)
    "gui-smoke-stress-switch": 1,          # only after the 20 s floor (stressSwitchReachesFloor + playHoldReachedFloor)
    "gui-smoke-measured-early-end": 1,     # a Play that ended under the floor is a typed failure, never a pass
}

# Entry points that must force Loop OFF (the autoplay hook is a lambda in the MainWindow constructor).
LOOP_OFF_ENTRY_MARKERS = (
    'forceLoopOffForAutomation( "autoplay" )',
    'forceLoopOffForAutomation( "profile-entry" )',
    'forceLoopOffForAutomation( "profile-look-assist-settle" )',
    'forceLoopOffForAutomation( "profile-exercise-play-action" )',
    'forceLoopOffForAutomation( "gui-smoke-entry" )',
    'forceLoopOffForAutomation( "gui-smoke-measured" )',
)

# The ONLY places the app may trigger / toggle the Play or Loop action programmatically. Each entry pins the
# enclosing function's signature, the exact (comment-stripped, whitespace-normalised) body, and how many
# trigger-class uses it holds. programmaticPlay's body is large, so it is pinned structurally (see the scan).
PINNED_HELPER_BODIES = {
    "void MainWindow::programmaticStop(":
        "{ Q_UNUSED( site ); if( ui->actionPlay->isChecked() ) ui->actionPlay->trigger(); }",
    "void MainWindow::forceLoopOffForAutomation(":
        '{ if( !ui->actionLoop->isChecked() ) return; logInteractionEvent( QStringLiteral("automation.loop_forced_off"), '
        'QStringLiteral("site=%1").arg( QString::fromLatin1( site ) ) ); ui->actionLoop->trigger(); }',
}
PROGRAMMATIC_PLAY_SIGNATURE = "bool MainWindow::programmaticPlay("

# Safe, reviewed forms: what may follow an occurrence of the action name.
_PLAY_SAFE_AFTER = re.compile(r"\s*->\s*(?:isChecked\s*\(\s*\)|setChecked\s*\(\s*false\s*\))")
# The context menu lists the action for the USER to click (user input is never gated).
_PLAY_SAFE_BEFORE = re.compile(r"addAction\s*\(\s*ui\s*->\s*$")
_LOOP_SAFE_AFTER = re.compile(r"\s*->\s*isChecked\s*\(\s*\)")
_LOOP_SAFE_BEFORE = re.compile(r"addAction\s*\(\s*ui\s*->\s*$")
# Handler calls that start Play: anything but a literal false (the definition / declaration take `bool`).
_HANDLER_START = re.compile(r"\bon_actionPlay_(?:triggered|toggled)\s*\(\s*(?!false\b|bool\b)\S")
_SET_PLAYING = re.compile(r"\bsetPlaying\s*\(")
_INVOKE_METHOD = re.compile(r"\binvokeMethod\s*\([^;]*?\w*[Aa]ctionPlay\w*")
_KEY_SYNTH = re.compile(r"\bQKeyEvent\s*(?:\w+\s*)?\(|\bKey_Space\b|\bQTest\s*::\s*key\w*|\bkeyClick\s*\(")
_EVENT_POST = re.compile(r"\b(?:postEvent|sendEvent|sendSpontaneousEvent)\s*\(")
# The one event dispatch that exists today (MyApplication's event filter forwarding to the main window).
PINNED_EVENT_SITES = {("platform/qt/MyApplication.h", "sendEvent"): 1}
_ACTION_PLAY = re.compile(r"\bactionPlay\b")
_ACTION_LOOP = re.compile(r"\bactionLoop\b")
_GATED_CALL = re.compile(r'programmaticPlay\s*\(\s*"([^"]+)"\s*,\s*([^;]*?)\)\s*[;){&|]', re.S)
_STOP_CALL = re.compile(r'programmaticStop\s*\(\s*"([^"]+)"')


def strip_cpp(text: str) -> tuple[str, str]:
    """(code with comments removed and string/char literals BLANKED, code with comments removed and literals kept).

    Both keep every newline so offsets map to line numbers. A tokenizer, not a regex: ``//`` inside a string is
    not a comment, ``/*`` inside a string is not a comment, escapes and raw strings are honoured.
    """
    blank: list[str] = []
    keep: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for ch in text[i:end]:
                if ch == "\n":
                    blank.append("\n")
                    keep.append("\n")
            blank.append(" ")
            keep.append(" ")
            i = end
            continue
        if c == "R" and nxt == '"':   # raw string R"delim( ... )delim"
            m = re.compile(r'R"([^()\\ ]{0,16})\(').match(text, i)
            if m:
                close = ")" + m.group(1) + '"'
                end = text.find(close, m.end())
                end = n if end < 0 else end + len(close)
                for ch in text[i:end]:
                    blank.append("\n" if ch == "\n" else " ")
                    keep.append(ch)
                i = end
                continue
        if c in "\"'":
            quote = c
            j = i + 1
            while j < n and text[j] != quote:
                if text[j] == "\\":
                    j += 1
                if j < n and text[j] == "\n":
                    break   # unterminated: stop at the line end rather than swallow the file
                j += 1
            j = min(n, j + 1)
            blank.append(quote)
            blank.extend("\n" if ch == "\n" else " " for ch in text[i + 1:j - 1])
            blank.append(quote)
            keep.append(text[i:j])
            i = j
            continue
        blank.append(c)
        keep.append(c)
        i += 1
    return "".join(blank), "".join(keep)


def _line_of(code: str, offset: int) -> int:
    return code.count("\n", 0, offset) + 1


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def function_span(code: str, signature: str) -> tuple[int, int] | None:
    """(start, end) offsets of the body of the function whose definition starts with ``signature``."""
    at = code.find(signature)
    if at < 0:
        return None
    brace = code.find("{", at)
    if brace < 0:
        return None
    depth = 0
    for index in range(brace, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return brace, index + 1
    return None


def function_body(text: str, signature: str) -> str:
    """The comment-stripped body (literals kept) of a function, '' when absent."""
    _, keep = strip_cpp(text)
    span = function_span(keep, signature)
    return keep[span[0]:span[1]] if span else ""


def scan_app_play_sources(sources: dict[str, str]) -> list[str]:
    problems: list[str] = []
    site_counts: dict[str, int] = {}
    site_args: dict[str, list[str]] = {}
    stop_counts: dict[str, int] = {}
    event_counts: dict[tuple[str, str], int] = {}

    main_window_text = sources.get("platform/qt/MainWindow.cpp", "")
    main_blank, main_keep = strip_cpp(main_window_text)
    allowed_spans: list[tuple[int, int, str]] = []   # spans in MainWindow.cpp where a trigger-class use is pinned

    # ---- the pinned helper bodies and the structurally pinned gate --------------------------------------
    for signature, expected in PINNED_HELPER_BODIES.items():
        span = function_span(main_keep, signature)
        if span is None:
            problems.append(f"pinned helper not found: {signature}")
            continue
        actual = _normalise(main_keep[span[0]:span[1]])
        if actual != expected:
            problems.append(f"pinned helper body changed (re-review it, then update the pin): {signature} => {actual}")
        allowed_spans.append((span[0], span[1], signature))

    gate_span = function_span(main_keep, PROGRAMMATIC_PLAY_SIGNATURE)
    if gate_span is None:
        problems.append("MainWindow::programmaticPlay not found")
    else:
        body = main_keep[gate_span[0]:gate_span[1]]
        normalised = _normalise(body)
        trigger_count = len(re.findall(r"actionPlay\s*->\s*trigger\s*\(", body))
        if trigger_count != 1:
            problems.append(f"programmaticPlay must hold exactly one Play trigger, found {trigger_count}")
        trigger_at = body.find("actionPlay->trigger()")
        # The gate, in order: evaluate the window, THEN (and only for a Play that is not already running with a
        # passing window) admit to the ledger, THEN trigger. Exact statements are pinned so a `false &&` or a
        # commented-out guard cannot survive.
        ordered = ("checkPlayableWindow( site, requestedSeconds );",
                   "if( alreadyPlaying && verdict.ok ) return true;",
                   "if( !m_programmaticPlayLedger.admit( verdict ) )")
        last = -1
        for needed in ordered:
            at = normalised.find(needed)
            if at < 0 or at < last:
                problems.append(f"programmaticPlay must contain, in order, the statement: {needed}")
            last = max(last, at)
        trigger_norm = normalised.find("actionPlay->trigger()")
        if trigger_norm < 0 or trigger_norm < last:
            problems.append("programmaticPlay must trigger Play only AFTER the window check and the ledger admission")
        if "return false" not in body[:trigger_at if trigger_at > 0 else len(body)]:
            problems.append("programmaticPlay has no refusal path before the Play trigger")
        # An already-running Play must not be adopted before the window is evaluated (fable ALREADY-PLAYING-1).
        early_return = re.search(r"actionPlay\s*->\s*isChecked\s*\(\s*\)\s*\)\s*return\s+true", body)
        if early_return:
            problems.append("programmaticPlay returns true for an already-running Play without evaluating the window")
        allowed_spans.append((gate_span[0], gate_span[1], PROGRAMMATIC_PLAY_SIGNATURE))

    # ---- every file: the whole-text scans ----------------------------------------------------------------
    for path, text in sources.items():
        blank, keep = (main_blank, main_keep) if path == "platform/qt/MainWindow.cpp" else strip_cpp(text)
        spans = allowed_spans if path == "platform/qt/MainWindow.cpp" else []

        def inside_pinned(offset: int) -> bool:
            return any(start <= offset < end for start, end, _ in spans)

        for match in _ACTION_PLAY.finditer(blank):
            if _PLAY_SAFE_AFTER.match(blank, match.end()) or _PLAY_SAFE_BEFORE.search(blank[max(0, match.start() - 40):match.start()]):
                continue
            if inside_pinned(match.start()):
                continue
            problems.append(
                f"{path}:{_line_of(blank, match.start())}: unreviewed use of the Play action (a trigger, an alias,"
                f" a connect, anything but isChecked()/setChecked(false) outside the pinned helpers): "
                f"{_normalise(blank[match.start():match.start() + 70])}")
        for match in _ACTION_LOOP.finditer(blank):
            if _LOOP_SAFE_AFTER.match(blank, match.end()) or _LOOP_SAFE_BEFORE.search(blank[max(0, match.start() - 40):match.start()]):
                continue
            if inside_pinned(match.start()):
                continue
            problems.append(
                f"{path}:{_line_of(blank, match.start())}: unreviewed use of the Loop action (anything but isChecked()"
                f" outside the one pinned unchecker): {_normalise(blank[match.start():match.start() + 70])}")
        for match in _HANDLER_START.finditer(blank):
            problems.append(f"{path}:{_line_of(blank, match.start())}: direct call of the Play handler that is not a stop")
        for match in _SET_PLAYING.finditer(blank):
            problems.append(f"{path}:{_line_of(blank, match.start())}: setPlaying( call")
        for match in _INVOKE_METHOD.finditer(keep):
            problems.append(f"{path}:{_line_of(keep, match.start())}: invokeMethod naming the Play action")
        for match in _KEY_SYNTH.finditer(blank):
            problems.append(f"{path}:{_line_of(blank, match.start())}: synthesized key event (the Play shortcut is Space)")
        for match in _EVENT_POST.finditer(blank):
            key = (path, re.sub(r"\W", "", match.group(0)))
            event_counts[key] = event_counts.get(key, 0) + 1
        for match in _GATED_CALL.finditer(keep):
            site_counts[match.group(1)] = site_counts.get(match.group(1), 0) + 1
            site_args.setdefault(match.group(1), []).append(_normalise(match.group(2)))
        for match in _STOP_CALL.finditer(keep):
            stop_counts[match.group(1)] = stop_counts.get(match.group(1), 0) + 1

    for key, count in event_counts.items():
        if PINNED_EVENT_SITES.get(key, 0) != count:
            problems.append(f"event dispatch {key[1]} in {key[0]} x{count}: not a pinned site (the Play shortcut can be synthesized)")

    # ---- the reviewed site tables ------------------------------------------------------------------------
    reviewed_counts = {site: count for site, (count, _) in REVIEWED_PROGRAMMATIC_PLAY_SITES.items()}
    if site_counts != reviewed_counts:
        problems.append(f"programmaticPlay sites {sorted(site_counts.items())} != reviewed {sorted(reviewed_counts.items())}")
    for site, (_, prefix) in REVIEWED_PROGRAMMATIC_PLAY_SITES.items():
        for argument in site_args.get(site, []):
            if not argument.startswith(prefix):
                problems.append(f"programmaticPlay site {site}: requested window `{argument}` must start with `{prefix}`")
    if stop_counts != REVIEWED_PROGRAMMATIC_STOP_SITES:
        problems.append(f"programmaticStop sites {sorted(stop_counts.items())} != reviewed "
                        f"{sorted(REVIEWED_PROGRAMMATIC_STOP_SITES.items())}")
    for marker in LOOP_OFF_ENTRY_MARKERS:
        if marker not in main_keep:
            problems.append(f"missing Loop-off at an automation entry: {marker}")
    # Interactive handlers are never gated.
    for signature in ("void MainWindow::on_actionPlay_triggered(bool checked)",
                      "void MainWindow::on_actionPlay_toggled(bool checked)"):
        span = function_span(main_keep, signature)
        if span and "programmaticPlay(" in main_keep[span[0]:span[1]]:
            problems.append(f"{signature} is a user-input handler and must not be gated")
    return problems
