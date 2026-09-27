"""GPU-TEXNR-S1-DARK-GREEN-1: the GL-window recon-texture route must never bind the
plain passthrough shader program again.

The regression this closes: GpuDisplayWindow drew the CUDA AMaZE post-WB-undo texture
(linear camera RGB) through its passthrough fragment shader (no LUT/WB/gamma math),
producing a flat dark grey-green picture. The fix makes GpuDisplayWindow::paintGL()
select between the shared preview-processing program (for a GPU-recon texture) and the
passthrough program (for an already display-referred QImage) via a single
`activeProgram` variable, rather than always drawing through `m_program`.

This is a text-level check, not an execution one: it proves the CODE SHAPE of the fix
(paintGL()'s draw calls go through the selected `activeProgram`, not the unconditional
`m_program`), which is what a future edit could silently regress by reintroducing a bare
`m_program->bind()`/`draw` in that function. It complements, not replaces, the GUI-gated
behavioural tests in tests/gui/test_gui_smoke.cpp that exercise the actual refusal and
the CPU-reference LUT math.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "platform" / "qt" / "GpuDisplayWindow.cpp"


def _function_body(text: str, signature: str) -> str:
    """Return the body of a top-level function, from its opening `{` to the matching
    top-level closing `}` (column 0), by brace depth -- simple and robust against the
    nested braces/strings this file's functions contain."""
    start = text.index(signature)
    open_brace = text.index("{", start)
    depth = 0
    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace:index + 1]
    raise AssertionError(f"unterminated function body for {signature!r}")


class GpuWindowReconShaderStaticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SOURCE.read_text(encoding="utf-8")

    def test_paint_gl_draws_through_a_selected_program_not_the_passthrough_member(self) -> None:
        body = _function_body(self.text, "void GpuDisplayWindow::paintGL()")

        # The recon/passthrough choice must exist and be draw-time, not baked in once.
        self.assertIn("presentingReconTexture", body)
        self.assertIn("m_previewProcessingProgram", body)
        self.assertIn("activeProgram", body)

        # The actual GL calls (bind/uniforms/draw/release) go through the selected
        # program, never unconditionally through the passthrough member.
        for offender in (
            "m_program->bind()",
            "m_program->setUniformValue",
            "m_program->enableAttributeArray",
            "m_program->release()",
        ):
            self.assertNotIn(
                offender,
                body,
                f"paintGL() must draw through activeProgram, not directly via {offender}",
            )
        self.assertIn("activeProgram->bind()", body)
        self.assertIn("activeProgram->release()", body)

    def test_recon_texture_submit_builds_the_shared_processing_program_not_ensure_program(self) -> None:
        body = _function_body(
            self.text,
            "bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture(",
        )

        self.assertIn("ensurePreviewProcessingProgram()", body)
        self.assertNotIn(
            "ensureProgram()",
            body,
            "the GPU-recon texture submit path must build the shared preview-processing "
            "program (ensurePreviewProcessingProgram), not the plain passthrough one",
        )

    def test_recon_submit_fails_closed_when_processing_options_are_unusable(self) -> None:
        body = _function_body(
            self.text,
            "bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture(",
        )
        self.assertIn("previewProcessingOptionsUsable", body)
        self.assertIn("gpu_window_recon_missing_processing_options", body)
        self.assertRegex(
            body,
            re.compile(r"if\s*\(\s*!previewProcessingOptionsUsable\s*\)"),
        )


if __name__ == "__main__":
    unittest.main()
