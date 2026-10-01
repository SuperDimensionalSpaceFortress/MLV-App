"""PLAYBACK-CLIP-LENGTH-ENFORCE-2 round 2: the static class scan over the app's C++ for programmatic Play.

A pure function over ``{path: text}`` so a test can MUTATE the real sources in memory and require the scan to go
red (a scan that passes on a deliberately broken tree is worthless).

What changed from round 1 (sol hardening): the allowlist is no longer "any line carrying an ``allowlisted:``
comment". The scan strips comments and string literals with a real tokenizer (a ``//`` inside a string cannot hide
code, a block comment cannot carry an exemption), matches over the WHOLE file (a line break between
``actionPlay`` and ``->trigger`` cannot split a match), and treats EVERY use of the Play / Loop action as
unreviewed unless it is one of a short list of safe forms or sits inside one of three PINNED helper bodies whose
exact text is compared with a pinned string. Adding an exemption therefore means editing this file, in review.

ENFORCE-3 (sol r2 hardening, owner rule 2026-10-01 "20 s means 20 s of source frames"): the scan now also pins

* the COMPLETE request expression of every ``programmaticPlay(`` call (not a prefix: ``kMinPlayWindowSeconds / 20.0``
  or ``kMinPlayWindowSeconds - 19`` no longer pass),
* EVERY automation stop site by enclosing function and count -- a raw ``actionPlay->setChecked( false )``,
  ``on_actionPlay_triggered( false )`` or ``programmaticStop(`` inserted anywhere (``playbackClock.restart()`` or
  not) changes a pinned count and is reported,
* the exact text of the statements that make each stop wait for the engine's source-frame count
  (``programmaticPlayState`` / ``programmaticPlayConsumed``), and the hold loops themselves (no extra ``break``, no
  stop call inside them),
* that no wall clock decides a Play (``playHoldReachedFloor`` is gone; no ``while`` mixes ``elapsed()`` and the
  Play action), and that the engine tick feeds the counter and the gate is given the engine's real pace.

Violations are returned as ``path:line: message`` strings; an empty list means the class is closed.
"""

from __future__ import annotations

import re

PLAY_SOURCES = ("platform/qt/MainWindow.cpp", "platform/qt/main.cpp")

# Every programmaticPlay( call site, with how many times it may appear and the COMPLETE (whitespace-normalised)
# REQUESTED-window expression it passes. A NEW site, a duplicated site, or ANY change to the request expression --
# not just its first token -- fails the scan.
REVIEWED_PROGRAMMATIC_PLAY_SITES = {
    "autoplay": (1, "autoplaySeconds"),                                          # MLVAPP_AUTOPLAY_* hook (normal GUI)
    "profile-look-assist-settle": (1, "playback_frame_range::kMinPlayWindowSeconds"),
    "profile-exercise-play-action": (1, "playback_frame_range::kMinPlayWindowSeconds"),
    "gui-smoke-measured": (1, "playback_frame_range::smokePlayRequestSeconds( options.durationMs, "
                              "options.targetPresentedFrames, presentedTargetFps )"),
}

# Every programmaticStop( call site (an automation mode that ENDS a Play) and how many times.
REVIEWED_PROGRAMMATIC_STOP_SITES = {
    "profile-look-assist-settle": 1,       # after the hold loop: programmaticPlayState != Continue (source frames consumed)
    "gui-smoke-stress-switch": 1,          # only once programmaticPlayConsumed() (the engine counted the source frames)
    "gui-smoke-measured-early-end": 1,     # a Play that did not consume them is a typed failure, never a pass
}

# ENFORCE-3: EVERY place that stops a Play, by enclosing MainWindow function: (raw ``actionPlay->setChecked( false )``,
# ``on_actionPlay_triggered( false )``, ``programmaticStop(``). A count that differs -- or a function that is not
# listed -- is a new stop site and fails the scan until it is reviewed HERE. The user-input handlers (close, open,
# export, import, session, resize, show-file) stop Play because the USER did something; the three automation entries
# (the autoplay poll in the constructor, the smoke, the profile) and the two engine stops are pinned separately below.
REVIEWED_PLAY_STOPS = {
    "MainWindow::MainWindow": (1, 1, 0),                              # autoplay poll lambda: after programmaticPlayState
    "MainWindow::closeEvent": (1, 1, 0),
    "MainWindow::resizeEvent": (1, 0, 0),
    "MainWindow::showFileInEditor": (1, 0, 0),
    "MainWindow::openDngFolderDialog": (1, 0, 0),
    "MainWindow::on_actionOpen_triggered": (1, 0, 0),
    "MainWindow::on_actionOpenSession_triggered": (1, 0, 0),
    "MainWindow::on_actionSaveSession_triggered": (1, 0, 0),
    "MainWindow::on_actionSaveAsSession_triggered": (1, 0, 0),
    "MainWindow::on_actionSaveSessionMetadata_triggered": (1, 0, 0),
    "MainWindow::on_actionImportReceipt_triggered": (1, 0, 0),
    "MainWindow::on_actionExportReceipt_triggered": (1, 0, 0),
    "MainWindow::on_actionExport_triggered": (1, 0, 0),
    "MainWindow::on_actionExportCurrentFrame_triggered": (1, 0, 0),
    "MainWindow::on_actionExportSettings_triggered": (1, 0, 0),
    "MainWindow::on_actionFcpxmlImportAssistant_triggered": (1, 0, 0),
    "MainWindow::on_actionUseDefaultReceipt_triggered": (1, 0, 0),
    "MainWindow::playbackHandling": (1, 0, 0),                        # the engine reached the last frame (no Loop)
    "MainWindow::notePlaybackSmokePresentedFrame": (1, 0, 0),         # presented-frames target AND programmaticPlayConsumed()
    "MainWindow::runGuiPlaybackSmoke": (1, 0, 2),                     # post-measurement stop; stress switch; early-end
    "MainWindow::runHeadlessPlaybackProfile": (1, 0, 1),              # play-action post-hold stop; settle stop
    "MainWindow::programmaticPlay": (0, 0, 1),                        # a refused already-running Play is stopped
}

# ENFORCE-3: the statements that make every automation stop WAIT for the engine's source-frame count, pinned as
# exact (comment-stripped, whitespace-normalised) text that must occur exactly once in the named function. Changing
# one -- adding a wall-clock alternative, an extra exit condition, a different request -- means editing this table.
PINNED_STOP_STATEMENTS = {
    "MainWindow::MainWindow": (
        "const playback_frame_range::PlayStopState autoplayState = programmaticPlayState( autoplayClock->elapsed(), "
        "playback_frame_range::playSafetyMs( m_playRequestedSeconds ) ); "
        "if( autoplayState == playback_frame_range::PlayStopState::Continue ) return; autoplayPoll->stop(); "
        "autoplayPoll->deleteLater(); if( ui->actionPlay->isChecked() ) { ui->actionPlay->setChecked( false ); "
        "on_actionPlay_triggered( false ); }",
        "isolateAutomationPacing( \"autoplay\" );",
    ),
    "MainWindow::runHeadlessPlaybackProfile": (
        "const qint64 autoSettleSafetyMs = playback_frame_range::playSafetyMs( m_playRequestedSeconds ); "
        "playback_frame_range::PlayStopState autoSettleState = playback_frame_range::PlayStopState::Continue; "
        "while( ( autoSettleState = programmaticPlayState( autoSettleClock.elapsed(), autoSettleSafetyMs ) ) "
        "== playback_frame_range::PlayStopState::Continue ) { qApp->processEvents( QEventLoop::AllEvents ); "
        "QThread::msleep( 10 ); } programmaticStop( \"profile-look-assist-settle\" ); "
        "if( autoSettleState != playback_frame_range::PlayStopState::Reached )",
        "const qint64 playActionSafetyMs = playback_frame_range::playSafetyMs( m_playRequestedSeconds ); "
        "while( ( playActionState = programmaticPlayState( playActionClock.elapsed(), playActionSafetyMs ) ) "
        "== playback_frame_range::PlayStopState::Continue ) { qApp->processEvents( QEventLoop::AllEvents ); "
        "QThread::msleep( 10 ); } playActionEndedEarly = playActionState == playback_frame_range::PlayStopState::EndedEarly;",
        "isolateAutomationPacing( \"profile-entry\" );",
    ),
    "MainWindow::runGuiPlaybackSmoke": (
        "measuredState = programmaticPlayState( playbackClock.elapsed(), measuredSafetyMs ); "
        "if( measuredState == playback_frame_range::PlayStopState::SafetyTimeout "
        "|| measuredState == playback_frame_range::PlayStopState::EndedEarly "
        "|| ( measuredState == playback_frame_range::PlayStopState::Reached "
        "&& !( options.exerciseClipLifecycleStress && !stressAttempted ) ) ) { break; }",
        "if( measuredState != playback_frame_range::PlayStopState::Reached ) { "
        "m_playbackSmokeFullscreenLossLatchArmed = false; programmaticStop( \"gui-smoke-measured-early-end\" );",
        "&& programmaticPlayConsumed() && ( playbackClock.elapsed() >= qMax( 0, options.stressSwitchAtMs ) "
        "|| !ui->actionPlay->isChecked() ) )",
        "isolateAutomationPacing( \"gui-smoke-entry\" );",
    ),
    "MainWindow::notePlaybackSmokePresentedFrame": (
        "if( m_playbackSmokeTargetPresentedFrames > 0 && m_playbackSmokePresentedFrames >= m_playbackSmokeTargetPresentedFrames "
        "&& programmaticPlayConsumed() && ui->actionPlay->isChecked() )",
    ),
    "MainWindow::checkPlayableWindow": (
        "return playback_frame_range::evaluatePlayableWindow( ui->horizontalSliderPosition->value(), "
        "ui->spinBoxCutIn->value(), ui->spinBoxCutOut->value(), totalFrames, fps, requestedSeconds, "
        "playback_frame_range::kMinPlayWindowSeconds, !f3CutRangeRepairDisabledByEnvironment(), "
        "enginePaceFps > 0.0 ? enginePaceFps : -1.0 );",
        "const double enginePaceFps = getFramerate();",
    ),
    "MainWindow::programmaticPlay": (
        "m_sourceAdvance = playback_frame_range::SourceFrameAdvanceCounter(); "
        "m_playRequiredSourceFrames = verdict.requiredFrames;",
    ),
    "MainWindow::getFramerate": (
        "if( m_fpsOverride && !m_automationPacingIsolated ) return m_frameRate;",
    ),
    "MainWindow::programmaticPlayState": (
        "return playback_frame_range::evaluatePlayStop( m_sourceAdvance.consumed(), m_playRequiredSourceFrames, "
        "ui->actionPlay->isChecked(), elapsedMs, safetyMs );",
    ),
    "MainWindow::programmaticPlayConsumed": (
        "return m_playRequiredSourceFrames > 0 && m_sourceAdvance.consumed() >= m_playRequiredSourceFrames;",
    ),
    "MainWindow::isolateAutomationPacing": (
        "m_automationPacingIsolated = true; if( !ui->actionDropFrameMode->isChecked() ) "
        "ui->actionDropFrameMode->setChecked( true );",
    ),
}

# ENFORCE-3: the loops an automation stop waits in. Each header must occur once in the function; the loop body
# (up to its matching brace) holds exactly this many ``break``s and NO call that stops or starts Play.
PINNED_WAIT_LOOPS = {
    "MainWindow::runGuiPlaybackSmoke": (("for( ;; )", 1),),
    "MainWindow::runHeadlessPlaybackProfile": (
        ("while( ( autoSettleState = programmaticPlayState(", 0),
        ("while( ( playActionState = programmaticPlayState(", 0),
    ),
}
_STOP_OR_START_IN_LOOP = re.compile(r"setChecked|programmaticStop|programmaticPlay\s*\(|\btrigger\s*\(|on_actionPlay_")

# ENFORCE-3: the engine tick feeds the source-frame counter exactly where the position advances (the Loop wrap, the
# non-drop step, the drop-frame step), and the gate is told the engine's pace.
PINNED_TICK_FEEDS = {"MainWindow::playbackHandling": 3}

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
_GATED_CALL = re.compile(r'\bprogrammaticPlay\s*\(\s*"([^"]+)"\s*,')
_WALL_CLOCK_PLAY_LOOP = re.compile(r"\bwhile\s*\([^{;]*\belapsed\s*\(\s*\)[^{;]*\bactionPlay\b|\bwhile\s*\([^{;]*\bactionPlay\b[^{;]*\belapsed\s*\(\s*\)")
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


def _call_second_argument(keep: str, open_paren: int) -> str:
    """The (normalised) text of the SECOND top-level argument of the call whose '(' is at ``open_paren``."""
    depth = 0
    commas: list[int] = []
    end = len(keep)
    for index in range(open_paren, len(keep)):
        ch = keep[index]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                end = index
                break
        elif ch == "," and depth == 1:
            commas.append(index)
    if not commas:
        return ""
    return _normalise(keep[commas[0] + 1:end])


_FUNCTION_START = re.compile(r"(?m)^(?:[A-Za-z_][^\n;{}()]*?\s)?(?:MainWindow)::(~?\w+)\s*\(")


def function_spans(blank: str) -> list[tuple[int, int, str]]:
    """(body start, body end, 'MainWindow::name') of every column-0 MainWindow:: definition in comment-stripped code."""
    spans: list[tuple[int, int, str]] = []
    for match in _FUNCTION_START.finditer(blank):
        depth = 0
        index = match.end() - 1
        while index < len(blank):
            if blank[index] == "(":
                depth += 1
            elif blank[index] == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        cursor = index + 1
        while cursor < len(blank) and blank[cursor] not in "{;":
            cursor += 1
        if cursor >= len(blank) or blank[cursor] == ";":
            continue
        depth = 0
        end = cursor
        while end < len(blank):
            if blank[end] == "{":
                depth += 1
            elif blank[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        spans.append((cursor, end + 1, "MainWindow::" + match.group(1)))
    return spans


def _enclosing(spans: list[tuple[int, int, str]], offset: int) -> str:
    for start, end, name in spans:
        if start <= offset < end:
            return name
    return "<file scope>"


def _matching_brace(code: str, open_brace: int) -> int:
    depth = 0
    for index in range(open_brace, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(code)


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
                   "if( alreadyPlaying && verdict.ok ) {",
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
            site_args.setdefault(match.group(1), []).append(_call_second_argument(keep, match.start() + match.group(0).index("(")))
        for match in _WALL_CLOCK_PLAY_LOOP.finditer(blank):
            problems.append(f"{path}:{_line_of(blank, match.start())}: a wait loop decides a Play on a wall clock (elapsed() with the Play action); "
                            "automation waits on programmaticPlayState (the engine's source-frame count)")
        for match in re.finditer(r"\bplayHoldReachedFloor\b", blank):
            problems.append(f"{path}:{_line_of(blank, match.start())}: playHoldReachedFloor is the retired wall-clock predicate")
        for match in _STOP_CALL.finditer(keep):
            stop_counts[match.group(1)] = stop_counts.get(match.group(1), 0) + 1

    for key, count in event_counts.items():
        if PINNED_EVENT_SITES.get(key, 0) != count:
            problems.append(f"event dispatch {key[1]} in {key[0]} x{count}: not a pinned site (the Play shortcut can be synthesized)")

    # ---- the reviewed site tables ------------------------------------------------------------------------
    reviewed_counts = {site: count for site, (count, _) in REVIEWED_PROGRAMMATIC_PLAY_SITES.items()}
    if site_counts != reviewed_counts:
        problems.append(f"programmaticPlay sites {sorted(site_counts.items())} != reviewed {sorted(reviewed_counts.items())}")
    for site, (_, expression) in REVIEWED_PROGRAMMATIC_PLAY_SITES.items():
        for argument in site_args.get(site, []):
            if argument != expression:
                problems.append(f"programmaticPlay site {site}: requested window `{argument}` must be exactly `{expression}`")
    if stop_counts != REVIEWED_PROGRAMMATIC_STOP_SITES:
        problems.append(f"programmaticStop sites {sorted(stop_counts.items())} != reviewed "
                        f"{sorted(REVIEWED_PROGRAMMATIC_STOP_SITES.items())}")
    for marker in LOOP_OFF_ENTRY_MARKERS:
        if marker not in main_keep:
            problems.append(f"missing Loop-off at an automation entry: {marker}")
    problems.extend(_scan_stop_sites(main_blank, main_keep))
    # Interactive handlers are never gated.
    for signature in ("void MainWindow::on_actionPlay_triggered(bool checked)",
                      "void MainWindow::on_actionPlay_toggled(bool checked)"):
        span = function_span(main_keep, signature)
        if span and "programmaticPlay(" in main_keep[span[0]:span[1]]:
            problems.append(f"{signature} is a user-input handler and must not be gated")
    return problems


def _scan_stop_sites(blank: str, keep: str) -> list[str]:
    """ENFORCE-3: every Play stop by enclosing function, the pinned wait statements, the wait loops, the tick feeds."""
    problems: list[str] = []
    if not blank:
        return problems
    spans = function_spans(blank)
    counts: dict[str, list[int]] = {}
    patterns = (
        re.compile(r"\bactionPlay\s*->\s*setChecked\s*\(\s*false\s*\)"),
        re.compile(r"\bon_actionPlay_triggered\s*\(\s*false\s*\)"),
        re.compile(r'(?<!::)\bprogrammaticStop\s*\('),
    )
    for slot, pattern in enumerate(patterns):
        for match in pattern.finditer(blank):
            counts.setdefault(_enclosing(spans, match.start()), [0, 0, 0])[slot] += 1
    for function, found in sorted(counts.items()):
        expected = REVIEWED_PLAY_STOPS.get(function)
        if expected is None:
            problems.append(f"unreviewed Play stop in {function}: raw setChecked(false) x{found[0]}, "
                            f"on_actionPlay_triggered(false) x{found[1]}, programmaticStop x{found[2]}")
        elif tuple(found) != expected:
            problems.append(f"Play stop sites in {function} are {tuple(found)} but the reviewed pin is {expected}")
    for function, expected in REVIEWED_PLAY_STOPS.items():
        if function not in counts and any(expected):
            problems.append(f"reviewed Play stop site vanished from {function} (pin {expected})")

    by_name = {name: (start, end) for start, end, name in spans}
    for function, statements in PINNED_STOP_STATEMENTS.items():
        span = by_name.get(function)
        if span is None:
            problems.append(f"pinned stop function not found: {function}")
            continue
        body = _normalise(keep[span[0]:span[1]])
        for statement in statements:
            occurrences = body.count(statement)
            if occurrences != 1:
                problems.append(f"{function}: pinned stop statement occurs {occurrences}x (must be exactly once): {statement[:110]}...")

    for function, loops in PINNED_WAIT_LOOPS.items():
        span = by_name.get(function)
        if span is None:
            problems.append(f"pinned wait-loop function not found: {function}")
            continue
        body = keep[span[0]:span[1]]
        for header, breaks in loops:
            at = body.find(header)
            if at < 0 or body.count(header) != 1:
                problems.append(f"{function}: wait loop `{header}` must occur exactly once")
                continue
            open_brace = body.find("{", at)
            region = body[at:_matching_brace(body, open_brace) + 1]
            found_breaks = len(re.findall(r"\bbreak\s*;", region))
            if found_breaks != breaks:
                problems.append(f"{function}: wait loop `{header}` holds {found_breaks} break(s), pinned {breaks}")
            if _STOP_OR_START_IN_LOOP.search(region):
                problems.append(f"{function}: wait loop `{header}` contains a call that stops or starts Play")

    for function, feeds in PINNED_TICK_FEEDS.items():
        span = by_name.get(function)
        found = keep[span[0]:span[1]].count("m_sourceAdvance.noteEngineTick(") if span else -1
        if found != feeds:
            problems.append(f"{function}: the engine tick feeds the source-frame counter {found}x, pinned {feeds}")
    return problems
