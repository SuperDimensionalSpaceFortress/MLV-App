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
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
MAIN_WINDOW_CPP = ROOT / "platform/qt/MainWindow.cpp"

SHARED_KEYS = "rawUint16FrameKeys"
PER_FRAME_KEYS = (
    "raw_uint16_source",
    "raw_uint16_frame_lj92_ms",
    "raw_uint16_inflight_wait_ms",
)
LAYOUT_KEYS = (
    "raw_uint16_lj92_predictor",
    "raw_uint16_lj92_precision",
    "raw_uint16_lj92_max_ssss",
    "raw_uint16_lj92_component_count",
    "raw_uint16_lj92_scan_component_count",
    "raw_uint16_lj92_pred1_fast_path_eligible",
    "raw_uint16_lj92_pred1_fast_path_active",
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

    def test_shared_string_carries_the_lj92_layout_keys(self) -> None:
        """LJ92-PRED1-FAST-PATH-1 r3: the layout of the decoded frame is read off the per-frame record, so a real
        clip's predictor, precision, largest SSSS and component counts show without a separate probe."""
        definition = self._shared_definition()
        for key in LAYOUT_KEYS:
            self.assertIn(" " + key + "=%", definition)
            self.assertIn('"' + key + '"', definition, "value must come from the frame's timing map")

    def test_render_thread_publishes_the_new_layout_keys(self) -> None:
        render = (ROOT / "platform/qt/RenderFrameThread.cpp").read_text(encoding="utf-8")
        for key in ("raw_uint16_lj92_precision", "raw_uint16_lj92_max_ssss"):
            self.assertIn('QStringLiteral("' + key + '")', render)

    def test_worker_decoded_frame_reports_the_workers_lj92_layout_not_the_render_threads(self) -> None:
        """LJ92-PRED1-FAST-PATH-1 r8 (adversary A1, introduced): drawFrame runs on the render thread, where the
        raw-uint16 thread-locals read 0 / -1 for a frame a DecodeWorker decoded. The record already takes the
        worker's timings when rawFromWorker; every lj92 layout / split getter must take the worker's snapshot the
        same way, or a worker-decoded frame reports its worker timings beside a cleared layout (precision 0,
        max SSSS -1). Each getMlvLastRawUint16Lj92* read must be a rawFromWorker ternary, and the capture helper
        must read the same getter."""
        render = (ROOT / "platform/qt/RenderFrameThread.cpp").read_text(encoding="utf-8")
        capture_start = render.index("void captureRawUint16DecodeTelemetry(")
        capture_end = render.index("\n}\n", capture_start)
        capture_body = render[capture_start:capture_end]
        getters = sorted(set(re.findall(r"getMlvLastRawUint16Lj92\w+\(\)", render)))
        self.assertGreaterEqual(len(getters), 20, "the lj92 getter family disappeared from the render thread")
        for getter in getters:
            self.assertIn(getter, capture_body, getter + " is not captured on the DecodeWorker")
            for match in re.finditer(re.escape(getter), render):
                if capture_start <= match.start() < capture_end:
                    continue
                statement_start = render.rindex(";", 0, match.start()) + 1
                self.assertIn(
                    "rawFromWorker", render[statement_start:match.start()],
                    getter + " is read from the render thread's thread-local even when the worker decoded the frame")

    def test_worker_snapshot_is_marked_valid_only_for_a_successful_acquisition(self) -> None:
        """LJ92-PRED1-FAST-PATH-1 r12 (adversary r11 A1, introduced major): the DecodeWorker captured its thread-locals
        whether or not getMlvRawFrameUint16 succeeded and marked the snapshot valid, so a failed acquisition handed the
        previous frame's layout to drawFrame as this frame's (rawFromWorker), even when the render thread then
        retried and decoded the frame itself. A snapshot is valid only for the decode that produced it."""
        render = (ROOT / "platform/qt/RenderFrameThread.cpp").read_text(encoding="utf-8")
        assignment = render.index("slot.reconProvenance.acquisitionSucceeded =\n            getMlvRawFrameUint16(")
        capture = render.index("captureRawUint16DecodeTelemetry( slot.rawDecodeTelemetry,", assignment)
        between = render[_statement_end(render, assignment) + 1:capture]
        self.assertRegex(
            between, r"if\s*\(\s*slot\.reconProvenance\.acquisitionSucceeded\s*\)",
            "the worker snapshot must be taken only when the acquisition succeeded")

    def test_every_early_return_of_the_raw_decode_follows_the_layout_reset(self) -> None:
        """LJ92-PRED1-FAST-PATH-1 r12 (adversary r11 A1 siblings video_mlv.c:3775/3791/3796): the layout getters read
        the last decode on the thread, so the reset sits at the top of getMlvRawFrameUint16Direct, ahead of the DNG
        return, the input-capacity refusals and the raw-frame malloc return, or a failed decode keeps the previous
        frame's layout."""
        source = (ROOT / "src/mlv/video_mlv.c").read_text(encoding="utf-8")
        start = source.index("static int getMlvRawFrameUint16Direct(mlvObject_t * video, uint64_t frameIndex, "
                             "uint16_t * unpackedFrame)\n{")
        first_return = source.index("return ", start)
        reset = source.find("g_mlv_last_raw_uint16_lj92_predictor = -1;", start)
        self.assertNotEqual(reset, -1)
        self.assertLess(reset, first_return, "a decode that returns early must not keep the previous layout")

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


class Lj92SlowHuffConfigurationTests(unittest.TestCase):
    """LJ92-PRED1-FAST-PATH-1 r11 (adversary A1, introduced major): the u64 predictor-1 reader read self->hufflut and
    self->huffbits unconditionally, but a -DSLOW_HUFF build compiles the ljp struct without those two members, so
    lj92.c stopped compiling in that (legacy, commented-out in the source, never defined by a project) configuration,
    which master still compiled. The legacy configuration stays a compilable one."""

    def _syntax_check(self, *defines: str) -> "subprocess.CompletedProcess[str]":
        compiler = shutil.which("gcc") or shutil.which("cc")
        if compiler is None:
            self.skipTest("no C compiler on PATH")
        return subprocess.run(
            [compiler, "-std=c99", "-fopenmp", "-fsyntax-only", *defines, "-I", str(ROOT / "src"),
             str(ROOT / "src/mlv/liblj92/lj92.c")],
            capture_output=True, text=True, timeout=120, check=False)

    def test_lj92_compiles_in_the_default_configuration(self) -> None:
        result = self._syntax_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lj92_compiles_with_slow_huff_defined(self) -> None:
        result = self._syntax_check("-DSLOW_HUFF")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
