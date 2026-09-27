"""Static tripwire: the playback_smoke.summary line must export present_nothing_drops.

CUDA-SCALE4-ZERO-PRESENT-1: presentPlaybackPreparedFrame's silent-drop path
(the static lexer tripwire for it is re-homed as card STATIC-PRESENT-TRIPWIRE-1;
see branch product/CUDA-SCALE4-ZERO-PRESENT-1 history for the reference
implementation) is now counted by m_presentNothingDropCount and must surface
in the same GUI-smoke summary line
that already reports prep_stale_drops/prep_generation_drops, so a scale-4
GL-window regression shows up as a nonzero counter instead of only a trace
line an operator has to go looking for.

Round 3: `PresentNothingDrops` appearing anywhere in the trailing .arg() chain
does not prove it fills the %64 placeholder specifically -- QString::arg()
fills numbered placeholders in ascending numeric order regardless of a call's
textual position, so the Nth .arg() call in source order always fills
placeholder %N (for a string whose placeholders are exactly 1..N, each used
once). The strengthened test verifies that contiguous-once property and then
checks the present_nothing_drops placeholder number's own .arg() call by
position, not just chain membership.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
MAIN_WINDOW_CPP = ROOT / "platform/qt/MainWindow.cpp"

PLACEHOLDER_PATTERN = re.compile(r"%(\d+)")
ARG_CALL_MARKER = ".arg("


def _matching_paren_end(text: str, open_paren_index: int) -> int:
    depth = 0
    index = open_paren_index
    while index < len(text):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise AssertionError("unterminated parenthesized expression")


def _split_arg_calls(chain: str):
    calls = []
    index = 0
    while True:
        start = chain.find(ARG_CALL_MARKER, index)
        if start == -1:
            break
        open_paren = start + len(ARG_CALL_MARKER) - 1
        close_paren = _matching_paren_end(chain, open_paren)
        calls.append(chain[open_paren + 1:close_paren])
        index = close_paren + 1
    return calls


class PlaybackSmokeSummaryPresentNothingDropsTests(unittest.TestCase):
    def test_summary_format_string_declares_the_field(self):
        source = MAIN_WINDOW_CPP.read_text(encoding="utf-8")
        self.assertIn("playback_smoke.summary", source)
        self.assertIn("present_nothing_drops=%", source)

    def test_summary_placeholder_position_maps_to_the_present_nothing_arg_call(self):
        source = MAIN_WINDOW_CPP.read_text(encoding="utf-8")
        field_index = source.index("present_nothing_drops=%")
        placeholder_end = field_index + len("present_nothing_drops=%")
        digits = ""
        while placeholder_end < len(source) and source[placeholder_end].isdigit():
            digits += source[placeholder_end]
            placeholder_end += 1
        self.assertTrue(digits, "present_nothing_drops=% is not followed by a placeholder number")
        target_placeholder = int(digits)

        # Isolate the QStringLiteral(...) call that owns this placeholder, so
        # the placeholder census below can't pick up unrelated %N text from
        # elsewhere in the file.
        literal_call_start = source.rindex("QStringLiteral(", 0, field_index)
        literal_open_paren = source.index("(", literal_call_start)
        literal_close_paren = _matching_paren_end(source, literal_open_paren)
        self.assertGreater(literal_close_paren, placeholder_end,
                            "present_nothing_drops=%N placeholder is not inside "
                            "the playback_smoke.summary QStringLiteral(...) call")
        format_string = source[literal_open_paren + 1:literal_close_paren]

        placeholder_numbers = sorted(
            int(match.group(1)) for match in PLACEHOLDER_PATTERN.finditer(format_string)
        )
        placeholder_count = len(placeholder_numbers)
        self.assertEqual(
            list(range(1, placeholder_count + 1)),
            placeholder_numbers,
            "playback_smoke.summary placeholders must be exactly %1.."
            f"%{placeholder_count}, each used exactly once -- otherwise "
            "arg-call position doesn't determine which placeholder is filled.",
        )
        self.assertIn(
            target_placeholder,
            range(1, placeholder_count + 1),
            f"present_nothing_drops=%{target_placeholder} is out of range for "
            f"a format string with {placeholder_count} placeholders",
        )

        # The format string and its .arg() chain both live in
        # finishPlaybackSmokeTelemetry; the field must resolve to the drop
        # counter, not some unrelated value left behind by a future edit.
        # source.index(");", ...) lands on the closing ')' that is immediately
        # followed by ';' -- include that character (it closes the outermost
        # .arg() call) but not the ';' itself.
        chain_end = source.index(");", literal_close_paren)
        arg_chain = source[literal_close_paren + 1:chain_end + 1]
        arg_calls = _split_arg_calls(arg_chain)
        self.assertEqual(
            placeholder_count,
            len(arg_calls),
            f"expected {placeholder_count} .arg() calls to match "
            f"{placeholder_count} placeholders, found {len(arg_calls)}",
        )

        # QString::arg() fills the lowest-numbered unfilled placeholder on
        # each call; since every placeholder 1..N is used exactly once, the
        # Kth .arg() call (1-indexed, source/evaluation order) always fills
        # placeholder %K -- so the present_nothing_drops call must be at
        # index (target_placeholder - 1).
        present_nothing_arg_call = arg_calls[target_placeholder - 1]
        self.assertIn(
            "PresentNothingDrops",
            present_nothing_arg_call,
            f"the .arg() call at position {target_placeholder} (which fills "
            f"%{target_placeholder}, i.e. present_nothing_drops) does not "
            f"reference PresentNothingDrops: {present_nothing_arg_call!r}",
        )

        # The chain reads a local snapshot of the counter rather than the
        # atomic directly; confirm that local is actually sourced from
        # m_presentNothingDropCount somewhere in the same function.
        function_start = source.rindex(
            "void MainWindow::finishPlaybackSmokeTelemetry", 0, field_index
        )
        function_slice = source[function_start:chain_end]
        self.assertIn("m_presentNothingDropCount", function_slice)


if __name__ == "__main__":
    unittest.main()
