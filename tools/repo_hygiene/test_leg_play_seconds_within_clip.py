"""CONFORM-LEG-PLAYSECONDS-WITHIN-CLIP-1 -- a tracked leg spec never asks for more footage than its clip has.

DEFECT (HUB-TICK 20261110T0014Z, D12): legs/m16-1456-conform-{windowed,fullscreen}-s1.json asked for
``playSeconds: 25`` of M16-1456, whose source is 969 frames at 59.94 fps = 16.17 s. Since the clip-length gate
(tools/profiling/gui-smoke-length-gate.ps1, Test-GuiSmokeClipLength: ``remaining < WindowSeconds`` is CLIP_TOO_SHORT)
every such leg is refused before Play (receipt outcome INVALID SMOKE_RUN_FAILED), and a whole Bachelor bench
produced no usable leg. Nothing in the repo compared a leg's window with its clip, so the spec sat in tree
unnoticed for a month.

THE GUARD: for every clip whose source length the repo TRACKS, every tracked leg spec for that clip has
``playSeconds`` (+ the start offset, if the spec carries one) <= the clip's source seconds. The gate measures the
window in SOURCE seconds at the clip's NATIVE fps, conformed or not (docs/playback-clip-length-rule.md), so the
native-fps length is the right bound for a conform leg too.

KNOWN LENGTHS are frame counts at the header fps, taken from the facts the repo already pins in
test_playback_clip_length_gate.py (test_a_conformed_high_frame_rate_clip_is_gated_in_source_seconds_at_its_native_fps:
"969 frames at 59.94 fps (M16-1456's shape) ... 1667 frames (M16-1457's)"). No footage is read. A clip with no
tracked length is SKIPPED and named in the output; a length is never invented.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGS = ROOT / "tools" / "profiling" / "dual-venue" / "legs"

# clipId -> (video frames in the MLVI header / VIDF blocks, native fps). Only what the repo itself pins.
KNOWN_CLIP_SOURCE = {
    "M16-1456": (969, 59.94),
    "M16-1457": (1667, 59.94),
}


def clip_seconds(clip_id: str) -> float:
    frames, fps = KNOWN_CLIP_SOURCE[clip_id]
    return frames / fps


def start_offset_seconds(spec: dict) -> float:
    """Seconds of footage skipped before Play, when the spec names a start frame (none does today)."""
    start = (spec.get("generatorArgs") or {}).get("startFrame")
    if isinstance(start, int) and not isinstance(start, bool) and start > 0 and spec["clipId"] in KNOWN_CLIP_SOURCE:
        return start / KNOWN_CLIP_SOURCE[spec["clipId"]][1]
    return 0.0


def window_overruns_clip(spec: dict) -> str | None:
    """A message when the spec's play window exceeds the footage left in its clip, else None (unknown clip -> None)."""
    clip_id = spec.get("clipId")
    if clip_id not in KNOWN_CLIP_SOURCE:
        return None
    remaining = clip_seconds(clip_id) - start_offset_seconds(spec)
    window = spec["playSeconds"]
    if window > remaining + 1e-9:
        return (f"{spec.get('legId')}: playSeconds={window} exceeds the {remaining:.2f} s of {clip_id} "
                f"({KNOWN_CLIP_SOURCE[clip_id][0]} frames at {KNOWN_CLIP_SOURCE[clip_id][1]} fps); "
                f"the app refuses it CLIP_TOO_SHORT before Play")
    return None


class LegPlaySecondsWithinClipTests(unittest.TestCase):
    def load_specs(self) -> dict:
        specs = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(LEGS.glob("*.json"))}
        self.assertGreater(len(specs), 0, "no tracked leg specs found")
        return specs

    def test_every_tracked_leg_window_fits_its_clip_where_the_length_is_known(self) -> None:
        specs = self.load_specs()
        offenders = [msg for spec in specs.values() if (msg := window_overruns_clip(spec))]
        self.assertEqual(offenders, [], "\n".join(offenders))
        # Say what was not checked: an unknown clip is skipped, never guessed.
        skipped = sorted({s["clipId"] for s in specs.values()} - set(KNOWN_CLIP_SOURCE))
        for clip in skipped:
            print(f"leg-play-seconds guard: SKIP clip {clip}: no tracked source length in the repo (not checked)")
        checked = sorted(n for n, s in specs.items() if s["clipId"] in KNOWN_CLIP_SOURCE)
        self.assertGreater(len(checked), 0, "the guard checked no leg: KNOWN_CLIP_SOURCE matches no spec")

    def test_the_hfr_conform_cells_on_m16_1456_play_a_window_the_gate_admits(self) -> None:
        """The two specs of the defect: >= 10 s (HFR floor at >= 50 fps) and inside the 16.17 s source."""
        for mode in ("windowed", "fullscreen"):
            spec = json.loads((LEGS / f"m16-1456-conform-{mode}-s1.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(spec["playSeconds"], 10, spec["legId"])
            self.assertLessEqual(spec["playSeconds"], int(clip_seconds("M16-1456")), spec["legId"])

    def test_the_guard_is_not_vacuous(self) -> None:
        """It flags the pre-fix shape, admits a window that fits, skips an unknown clip and honours a start offset."""
        self.assertIsNotNone(window_overruns_clip({"legId": "x", "clipId": "M16-1456", "playSeconds": 25}))
        self.assertIsNotNone(window_overruns_clip({"legId": "x", "clipId": "M16-1456", "playSeconds": 17}))
        self.assertIsNone(window_overruns_clip({"legId": "x", "clipId": "M16-1456", "playSeconds": 16}))
        self.assertIsNone(window_overruns_clip({"legId": "x", "clipId": "M16-1457", "playSeconds": 25}))
        self.assertIsNone(window_overruns_clip({"legId": "x", "clipId": "M16-1243", "playSeconds": 3000}))
        self.assertIsNotNone(window_overruns_clip(
            {"legId": "x", "clipId": "M16-1457", "playSeconds": 25, "generatorArgs": {"startFrame": 169}}))
        self.assertIsNone(window_overruns_clip(
            {"legId": "x", "clipId": "M16-1457", "playSeconds": 25, "generatorArgs": {"startFrame": 168}}))


if __name__ == "__main__":
    unittest.main()
