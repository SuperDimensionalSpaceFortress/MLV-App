"""Static tripwire: the CUDA texture route's per-frame record carries the raw-uint16 keys.

PLAYBACK-LJ92-DECODE-THROUGHPUT-1 round 3 (sol r2 BLOCKER cuda_per_frame_lj92_missing): the three
per-frame keys raw_uint16_source, raw_uint16_frame_lj92_ms and raw_uint16_inflight_wait_ms were
appended to playback_smoke.cpu_frame only. A CUDA texture-route series has to read them from the
GPU-route record, playback_smoke.gpu_frame, as well. Both lines append one shared string built from
the same slot.stageTimingTelemetry values, so the two records can never disagree.

The start line also logs MLVAPP_RAW_UINT16_PREFETCH_DECODERS, the lever's K knob, beside the
existing MLVAPP_DISABLE_RAW_UINT16_PREFETCH.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
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

    # A 1x1 8-bit predictor-1 frame whose DHT holds the symbols {0, 1} at code length 2 (so the largest SSSS is 1).
    _SLOW_HUFF_DRIVER = r"""
#include <stdint.h>
#include <stdio.h>
#include "mlv/liblj92/lj92.h"
int main(void)
{
    uint8_t frame[] = {
        0xFF, 0xD8,
        0xFF, 0xC4, 0x00, 0x15, 0x00, 0,2,0,0,0,0,0,0,0,0,0,0,0,0,0,0, 0x00, 0x01,
        0xFF, 0xC3, 0x00, 0x0B, 0x08, 0x00, 0x01, 0x00, 0x01, 0x01, 0x01, 0x11, 0x00,
        0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x01, 0x00, 0x00,
        0x00, 0x00, 0xFF, 0xD9 };
    lj92 h = NULL;
    int w = 0, hh = 0, bits = 0, comps = 0;
    uint16_t out[1] = { 0xA5A5u };
    if (lj92_open(&h, frame, (int)sizeof(frame), &w, &hh, &bits, &comps) != LJ92_ERROR_NONE) { puts("open failed"); return 2; }
    if (lj92_decode(h, out, 1, 0, NULL, 0) != LJ92_ERROR_NONE) { puts("decode failed"); return 3; }
    printf("max_ssss=%d\n", lj92_get_last_huff_max_ssss());
    lj92_close(h);
    return lj92_get_last_huff_max_ssss() == 1 ? 0 : 1;
}
"""

    def _build_and_run_slow_huff_driver(self, *defines: str) -> "subprocess.CompletedProcess[str]":
        compiler = shutil.which("gcc") or shutil.which("cc")
        if compiler is None:
            self.skipTest("no C compiler on PATH")
        with tempfile.TemporaryDirectory() as scratch:
            driver = Path(scratch) / "slow_huff_driver.c"
            driver.write_text(self._SLOW_HUFF_DRIVER, encoding="utf-8")
            exe = Path(scratch) / ("slow_huff_driver.exe" if os.name == "nt" else "slow_huff_driver")
            build = subprocess.run(
                [compiler, "-std=c99", "-fopenmp", *defines, "-I", str(ROOT / "src"), str(driver),
                 str(ROOT / "src/mlv/liblj92/lj92.c"), "-o", str(exe)],
                capture_output=True, text=True, timeout=240, check=False)
            self.assertEqual(build.returncode, 0, build.stderr)
            return subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)

    def test_last_huff_max_ssss_is_reported_in_the_default_configuration(self) -> None:
        result = self._build_and_run_slow_huff_driver()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_last_huff_max_ssss_is_reported_with_slow_huff_defined(self) -> None:
        """LJ92-PRED1-DECODER-ONLY-1 r2b (sol r1 blocker S4): the new getter stayed -1 in a -DSLOW_HUFF build even though
        the DHT names a maximum symbol; the scan is stamped in both ljp variants."""
        result = self._build_and_run_slow_huff_driver("-DSLOW_HUFF")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @staticmethod
    def _platform_scopes(pro_text: str) -> "dict[str, str]":
        """Top-level `name { ... }` qmake scopes of a .pro file, name -> body (nested scopes stay in the body)."""
        scopes: "dict[str, str]" = {}
        depth = 0
        name = ""
        body_start = 0
        header_start = 0
        for index, char in enumerate(pro_text):
            if char == "{":
                if depth == 0:
                    name = pro_text[header_start:index].strip()
                    body_start = index + 1
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    scopes[name] = pro_text[body_start:index]
                    header_start = index + 1
            elif char == "\n" and depth == 0:
                header_start = index + 1
        return scopes

    def test_standalone_lj92_targets_link_openmp_on_every_declared_platform(self) -> None:
        """LJ92-PRED1-DECODER-ONLY-1 r1 (adversary A1, introduced major): lj92.c reaches omp_get_wtime through
        mlv_stage_timing_now's fallbacks (src/debug/StageTiming.h), so a standalone target that compiles it needs OpenMP
        on its compile AND link line in every platform scope it declares, as pipeline_tests.pro does. The reader tool's
        win32 scope had it; its linux-g++* and macx scopes, and every scope of the fuzz target, did not."""
        for relative in ("tests/console/lj92_pred1_reader_tool.pro", "tests/fuzz/fuzz_lj92.pro"):
            scopes = self._platform_scopes((ROOT / relative).read_text(encoding="utf-8"))
            for platform in ("win32", "linux-g++*", "macx"):
                self.assertIn(platform, scopes, relative + " lost its " + platform + " scope")
                body = scopes[platform]
                self.assertRegex(body, r"QMAKE_CFLAGS\s*\+=[^\n]*-fopenmp", relative + " " + platform + ": C compile lacks -fopenmp")
                self.assertRegex(
                    body, r"(QMAKE_LFLAGS\s*\+=[^\n]*-fopenmp|LIBS\s*\+=[^\n]*-l(libgomp-1|gomp|omp)\b)",
                    relative + " " + platform + ": link line lacks OpenMP")

    @staticmethod
    def _macx_arch_branches(macx_body: str) -> "dict[str, str]":
        """QT_ARCH branches of a macx scope body, arch -> branch body. Reads exactly the lines of the form
        `equals(QT_ARCH, <arch>) {` ... matching `}` (both .pro files name x86_64 and arm64 this way)."""
        branches: "dict[str, str]" = {}
        for opener in re.finditer(r"equals\(\s*QT_ARCH\s*,\s*([A-Za-z0-9_]+)\s*\)\s*\{", macx_body):
            depth = 1
            index = opener.end()
            while depth and index < len(macx_body):
                depth += {"{": 1, "}": -1}.get(macx_body[index], 0)
                index += 1
            branches[opener.group(1)] = macx_body[opener.end():index - 1]
        return branches

    def test_standalone_lj92_targets_pin_openmp_in_each_macos_arch_branch(self) -> None:
        """LJ92-PRO-MACOS-ARCH-PIN-1 r1 (Dot advisory 9dab5e8d on #880): the test above reads the WHOLE macx body, so
        a flag that survives in only one QT_ARCH branch still passes and the other macOS architecture can lose OpenMP
        while CI stays green. Each architecture the .pro names must carry the compile flag and the link flag itself."""
        for relative in ("tests/console/lj92_pred1_reader_tool.pro", "tests/fuzz/fuzz_lj92.pro"):
            scopes = self._platform_scopes((ROOT / relative).read_text(encoding="utf-8"))
            self.assertIn("macx", scopes, relative + " lost its macx scope")
            branches = self._macx_arch_branches(scopes["macx"])
            for arch in ("x86_64", "arm64"):
                self.assertIn(arch, branches, relative + " macx lost its equals(QT_ARCH, " + arch + ") branch")
            for arch, body in branches.items():
                self.assertRegex(
                    body, r"QMAKE_CFLAGS\s*\+=[^\n]*-fopenmp",
                    relative + " macx QT_ARCH=" + arch + ": C compile lacks -fopenmp")
                self.assertRegex(
                    body, r"(QMAKE_LFLAGS\s*\+=[^\n]*-fopenmp|LIBS\s*\+=[^\n]*-l(libgomp-1|gomp|omp)\b)",
                    relative + " macx QT_ARCH=" + arch + ": link line lacks OpenMP")


if __name__ == "__main__":
    unittest.main()
