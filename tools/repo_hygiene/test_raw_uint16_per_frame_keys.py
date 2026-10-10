"""Static tripwire: the CUDA texture route's per-frame record carries the raw-uint16 keys.

PLAYBACK-LJ92-DECODE-THROUGHPUT-1 round 3 (sol r2 BLOCKER cuda_per_frame_lj92_missing): the three
per-frame keys raw_uint16_source, raw_uint16_frame_lj92_ms and raw_uint16_inflight_wait_ms were
appended to playback_smoke.cpu_frame only. A CUDA texture-route series has to read them from the
GPU-route record, playback_smoke.gpu_frame, as well. Both lines append one shared string built from
the same slot.stageTimingTelemetry values, so the two records can never disagree.

The start line also logs MLVAPP_RAW_UINT16_PREFETCH_DECODERS, the lever's K knob, beside the
existing MLVAPP_DISABLE_RAW_UINT16_PREFETCH.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
MAIN_WINDOW_CPP = ROOT / "platform/qt/MainWindow.cpp"

SHARED_KEYS = "rawUint16FrameKeys"
PER_FRAME_KEYS = (
    "raw_uint16_source",
    "raw_uint16_frame_lj92_ms",
    "raw_uint16_inflight_wait_ms",
)


def _statement_end(text: str, start: int) -> int:
    """Index of the ';' that ends the statement starting at start (skips literals and comments)."""
    depth = 0
    index = start
    while index < len(text):
        ch = text[index]
        if text.startswith("//", index):
            index = text.index("\n", index)
            continue
        if text.startswith("/*", index):
            index = text.index("*/", index) + 2
            continue
        if ch in "\"'":
            index += 1
            while text[index] != ch:
                index += 2 if text[index] == "\\" else 1
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth == 0:
            return index
        index += 1
    raise AssertionError("unterminated statement")


def _statement_containing(text: str, marker: str, opener: str) -> str:
    marker_end = text.index(marker) + len(marker)
    start = text.rindex(opener, 0, marker_end)
    return text[start:_statement_end(text, start) + 1]


class RawUint16PerFrameKeysTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MAIN_WINDOW_CPP.read_text(encoding="utf-8")

    def _shared_definition(self) -> str:
        marker = "const QString " + SHARED_KEYS + " ="
        self.assertIn(marker, self.source)
        return _statement_containing(self.source, marker, marker)

    def test_shared_string_carries_all_three_keys_from_stage_timing(self) -> None:
        definition = self._shared_definition()
        for key in PER_FRAME_KEYS:
            self.assertIn(" " + key + "=%", definition)
            self.assertIn('"' + key + '"', definition, "value must come from the frame's timing map")

    def test_gpu_frame_record_appends_the_keys(self) -> None:
        statement = _statement_containing(self.source, '"playback_smoke.gpu_frame ', "qInfo()")
        self.assertIn("+ " + SHARED_KEYS, statement)
        self.assertLess(
            self.source.index("const QString " + SHARED_KEYS + " ="),
            self.source.index('"playback_smoke.gpu_frame '),
            "the shared string must be built before the gpu_frame record uses it")

    def test_cpu_frame_record_appends_the_same_keys(self) -> None:
        statement = _statement_containing(self.source, '"playback_smoke.cpu_frame ', "qInfo()")
        self.assertIn("+ " + SHARED_KEYS, statement)

    def test_start_line_logs_the_prefetch_decoder_knob(self) -> None:
        statement = _statement_containing(self.source, '"playback_smoke.start ', "qInfo()")
        self.assertIn("env_raw_uint16_prefetch_decoders=%", statement)
        self.assertIn('envValueForLog( "MLVAPP_RAW_UINT16_PREFETCH_DECODERS" )', statement)


if __name__ == "__main__":
    unittest.main()
