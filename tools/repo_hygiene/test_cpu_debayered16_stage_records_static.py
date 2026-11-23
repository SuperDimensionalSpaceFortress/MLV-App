"""CPU-S1-1457-HEAVY-SPEC-AND-STALE-FIELDS-1 (adversary A1): the CPU Debayered16 entries set the per-thread debayered-frame record, but must not
also PUBLISH a "debayered_frame" stage-timing record. getMlvRawFrameDebayered is called from inside getMlvProcessedFrame16_with_scale, which
publishes "debayered_frame" for the same interval around the call, so a second publish from the inner entry double-counts the nested work in the
stage-timing log and the export-evidence containment check (EXPORT_STAGE_CONTAINMENT_MISMATCH). The static source check below pins that.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "mlv" / "video_mlv.c"

ENTRIES = ("getMlvRawFrameDebayered", "getMlvRawFrameDebayeredFromReconnedRaw16", "getMlvRawFrameDebayeredFromReducedReconnedRaw16")


def function_body(text: str, name: str) -> str:
    match = re.search(r"^(?:int|void) " + re.escape(name) + r"\(", text, re.M)
    assert match, name
    start = text.index("{", match.end())
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError("unterminated " + name)


class CpuDebayered16StageRecordsStaticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SRC.read_text(encoding="utf-8").replace("\r\n", "\n")

    def test_each_entry_sets_the_thread_record_and_resets_processing(self) -> None:
        for name in ENTRIES:
            body = function_body(self.text, name)
            with self.subTest(name):
                self.assertRegex(body, r"g_mlv_last_debayered_frame_ms = 0\.0;")
                self.assertRegex(body, r"g_mlv_last_processing_ms = 0\.0;")
                self.assertRegex(body, r"g_mlv_last_debayered_frame_ms = \(mlv_stage_timing_now\(\) - debayer_start\) \* 1000\.0;")

    def test_no_entry_publishes_a_nested_debayered_frame_stage_record(self) -> None:
        for name in ENTRIES:
            with self.subTest(name):
                self.assertNotIn('mlv_stage_timing_note_elapsed("debayered_frame"', function_body(self.text, name))


if __name__ == "__main__":
    unittest.main()
