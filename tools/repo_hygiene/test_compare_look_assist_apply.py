"""LOOK-ASSIST-CLIP-OPEN-GUI-BLOCK-1: the comparator that proves the analysis landed on the same look."""
import importlib.util
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SPEC = importlib.util.spec_from_file_location(
    "compare_look_assist_apply", os.path.join(_ROOT, "tools", "profiling", "compare_look_assist_apply.py")
)
compare = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compare)


def line(stamp, thread, event, body):
    return "[2026-11-05T10:39:%sZ] [INFO] [%s] interaction_trace event=%s %s" % (stamp, thread, event, body)


def result(post_walk_ran="1", surface_search="not-run", final_temp="5808", last_serial="2", next_serial="4"):
    return (
        "analysis=raw scene=night median=32.000 preset_exp=0 preset_temp_delta=-192 preset_tint_delta=8 "
        "final_temp=%s final_tint=8 frame=0 last_serial=%s last_frame=0 next_serial=%s post_walk_ran=%s "
        "post_walk_branch=cleanup surface_search=%s flavor=cinematic"
    ) % (final_temp, last_serial, next_serial, post_walk_ran, surface_search)


def post_balance(post_temp_delta="768"):
    return "valid=1 warning=none balance_r=31.000 post_temp_delta=%s post_tint_delta=39 final_temp_delta=-192" % post_temp_delta


def visual(enabled="1", temperature="5808", gpu="1"):
    return (
        "look_assist_enabled=%s look_assist_diagnostics_valid=1 look_assist_scene=night exposure=0 contrast=34 pivot=43 "
        "shadows=22 highlights=-28 vibrance=7 temperature=%s tint=8 raw_black=20470 raw_white=16200 chroma_smooth=0 "
        "gpu16_preview_active=%s look_assist_flavor=cinematic"
    ) % (enabled, temperature, gpu)


def arm(thread="0xdcb8", stamp_offset=0, result_line=None, post_line=None, visual_line=None, play_first=False):
    """A whole log. stamp_offset moves every timestamp."""
    s = lambda n: "%06.3f" % (n + stamp_offset)
    lines = []
    if play_first:
        lines.append(line(s(0), thread, "play.toggled.begin", "checked=1 position=0"))
    lines += [
        line(s(1), thread, "look_assist.toggle.frame_ready_apply", "baseline_valid=1 analysis_frame=0"),
        line(s(2), thread, "look_assist.display_meter", "samples=1 robust_median=1.0 frame=0"),
        line(s(3), thread, "look_assist.window_lit_interior", "would_reclassify=0 reason=dim-patch surface_search=not-run"),
        line(s(4), thread, "look_assist.apply.auto_wb", "valid=1 candidate_temp=5040 final_temp_delta=-192"),
        line(s(5), thread, "look_assist.apply.color_balance", "source=processed balance_r=36.000"),
        line(s(6), thread, "look_assist.apply.post_balance", post_line or post_balance()),
        line(s(7), thread, "look_assist.apply.result", result_line or result()),
        line(s(8), thread, "look_assist.toggle.end", "checked=1 scene=night"),
        line(s(9), thread, "gui_smoke.look_assist_settle", "enabled=1 diagnostics_valid=1 wait_ms=3333"),
        line(s(10), thread, "gui_smoke.visual_state", visual_line or visual()),
    ]
    if not play_first:
        lines.append(line(s(11), thread, "play.toggled.begin", "checked=1 position=0"))
    return "\n".join(lines) + "\n"


class CompareLookAssistApply(unittest.TestCase):
    def test_same_analysis_on_another_thread_and_time_is_identical(self):
        report = compare.run(arm(), arm(thread="0x1234", stamp_offset=20))
        self.assertEqual(report["verdict"], "identical", report)

    def test_excluded_serials_do_not_decide(self):
        report = compare.run(arm(), arm(result_line=result(last_serial="3", next_serial="9")))
        self.assertEqual(report["verdict"], "identical", report)

    def test_a_changed_final_temperature_differs(self):
        report = compare.run(arm(), arm(result_line=result(final_temp="5809")))
        self.assertEqual(report["verdict"], "differs")
        self.assertTrue(any("final_temp" in d for d in report["differences"]), report)

    def test_a_visual_state_look_field_differs_but_a_route_field_does_not(self):
        self.assertEqual(compare.run(arm(), arm(visual_line=visual(temperature="5900")))["verdict"], "differs")
        self.assertEqual(compare.run(arm(), arm(visual_line=visual(gpu="0")))["verdict"], "identical")

    def test_a_missing_event_line_differs(self):
        kept = [l for l in arm().splitlines() if "look_assist.display_meter" not in l]
        report = compare.run(arm(), "\n".join(kept))
        self.assertEqual(report["verdict"], "differs")
        self.assertTrue(any("display_meter" in d for d in report["differences"]), report)

    def test_doctored_log_with_the_walk_skipped_goes_red(self):
        # The refinement deleted: no walk, no post delta, and a different final temperature.
        doctored = arm(
            result_line=result(post_walk_ran="0", final_temp="5040"),
            post_line=post_balance(post_temp_delta="0"),
        )
        report = compare.run(arm(), doctored, require_walk=True)
        self.assertEqual(report["verdict"], "differs")
        self.assertTrue(any("post_walk_ran=0" in f for f in report["failures"]), report)
        self.assertTrue(any("final_temp" in d for d in report["differences"]), report)
        self.assertTrue(any("post_temp_delta" in d for d in report["differences"]), report)

    def test_require_walk_fails_when_both_arms_agree_without_a_walk(self):
        same = arm(result_line=result(post_walk_ran="0"))
        self.assertEqual(compare.run(same, same)["verdict"], "identical")
        self.assertEqual(compare.run(same, same, require_walk=True)["verdict"], "differs")

    def test_require_surface_search_needs_a_converged_search(self):
        searched = arm(result_line=result(surface_search="converged"))
        self.assertEqual(compare.run(searched, searched, require_surface_search=True)["verdict"], "identical")
        self.assertEqual(compare.run(arm(), arm(), require_surface_search=True)["verdict"], "differs")

    def test_look_assist_off_fails_closed(self):
        off = arm(visual_line=visual(enabled="0"))
        report = compare.run(off, off)
        self.assertEqual(report["verdict"], "differs")
        self.assertTrue(any("look_assist_enabled=0" in f for f in report["failures"]), report)

    def test_an_arm_with_no_analysis_fails_closed(self):
        self.assertEqual(compare.run("", "")["verdict"], "differs")

    def test_adversary_a3_an_empty_result_and_an_invalid_state_fail_closed(self):
        log = (
            "[2026-11-05T10:39:01.000Z] [INFO] [0x1] interaction_trace event=look_assist.apply.result\n"
            "[2026-11-05T10:39:02.000Z] [INFO] [0x1] interaction_trace event=gui_smoke.visual_state "
            "look_assist_enabled=1 look_assist_diagnostics_valid=0\n"
        )
        report = compare.run(log, log)
        self.assertEqual(report["verdict"], "differs")
        self.assertTrue(any("has no final_temp" in f for f in report["failures"]), report)
        self.assertTrue(any("look_assist_diagnostics_valid=0" in f for f in report["failures"]), report)
        self.assertTrue(any("has no temperature" in f for f in report["failures"]), report)

    def test_adversary_a4_events_of_different_types_exchanged_differ(self):
        lines = arm().splitlines()
        post = next(i for i, l in enumerate(lines) if "look_assist.apply.post_balance" in l)
        res = next(i for i, l in enumerate(lines) if "look_assist.apply.result" in l)
        lines[post], lines[res] = lines[res], lines[post]
        report = compare.run(arm(), "\n".join(lines) + "\n", require_walk=True)
        self.assertEqual(report["verdict"], "differs")
        self.assertTrue(any(d.startswith("event order:") for d in report["differences"]), report)

    def test_play_before_the_analysis_landed_fails(self):
        report = compare.run(arm(), arm(play_first=True))
        self.assertEqual(report["verdict"], "differs")
        self.assertTrue(any("comes before the analysis landed" in f for f in report["failures"]), report)

    def test_adversary_a2_a_play_before_toggle_end_fails_even_after_the_result_line(self):
        # The toggle route lands at toggle.end: setReceipt and the refresh come after apply.result.
        lines = [l for l in arm().splitlines() if "play.toggled.begin" not in l]
        at = next(i for i, l in enumerate(lines) if "look_assist.toggle.end" in l)
        lines.insert(at, line("07.500", "0xdcb8", "play.toggled.begin", "checked=1 position=0"))
        report = compare.run(arm(), "\n".join(lines) + "\n")
        self.assertEqual(report["verdict"], "differs")
        self.assertTrue(any("comes before the analysis landed" in f for f in report["failures"]), report)

    def test_adversary_a2_a_finished_clip_load_analysis_does_not_land_a_later_toggle(self):
        load = line("00.100", "0xdcb8", "look_assist.setSliders.frame_ready_apply", "analysis_frame=0")
        load_result = line("00.200", "0xdcb8", "look_assist.apply.result", result())
        lines = [l for l in arm().splitlines() if "play.toggled.begin" not in l]
        at = next(i for i, l in enumerate(lines) if "look_assist.apply.result" in l)
        lines.insert(at + 1, line("07.500", "0xdcb8", "play.toggled.begin", "checked=1 position=0"))
        failure = compare.play_after_landing_failure(compare.parse_log("\n".join([load, load_result] + lines) + "\n"), "arm")
        self.assertIn("comes before the analysis landed", failure or "")

    def test_adversary_a3_the_clip_load_route_has_no_toggle_end(self):
        log = arm().replace("look_assist.toggle.frame_ready_apply", "look_assist.setSliders.frame_ready_apply")
        log = log.replace("look_assist.toggle.end", "look_assist.setSliders.end")
        self.assertEqual(compare.run(log, log)["verdict"], "identical")
        self.assertEqual(compare.measures(compare.parse_log(log))["result_gap_ms"], 6000.0)

    def test_adversary_a4_empty_values_and_an_earlier_empty_result_fail_closed(self):
        empty_state = (
            "look_assist_enabled=1 look_assist_diagnostics_valid=1 look_assist_scene= exposure= contrast= pivot= shadows= "
            "highlights= vibrance= temperature= tint= raw_black= raw_white= chroma_smooth= look_assist_flavor="
        )
        log = arm(visual_line=empty_state)
        report = compare.run(log, log)
        self.assertEqual(report["verdict"], "differs")
        self.assertTrue(any("has no temperature" in f for f in report["failures"]), report)
        early = line("00.000", "0x1", "look_assist.apply.result", "") + "\n" + arm()
        self.assertEqual(compare.run(early, early)["verdict"], "differs")

    def test_adversary_a5_an_unreadable_measure_does_not_stop_the_report(self):
        log = arm().replace("wait_ms=3333", "wait_ms=1.5")
        report = compare.run(log, log)
        self.assertEqual(report["verdict"], "identical")
        self.assertIsNone(report["control"]["wait_ms"])
        bad_stamp = arm().replace("2026-11-05T10:39:08", "not-a-stamp")
        self.assertIsNone(compare.measures(compare.parse_log(bad_stamp)).get("toggle_gap_ms"))

    def test_measures_read_wait_and_the_control_block(self):
        got = compare.measures(compare.parse_log(arm()))
        self.assertEqual(got["wait_ms"], 3333)
        self.assertEqual(got["toggle_gap_ms"], 7000.0)
        self.assertNotIn("timing", got)
        timed = arm() + line("30.000", "0x1", "look_assist.apply.timing", "total_ms=12.0 gui_block_max_ms=4.0 route=worker") + "\n"
        self.assertEqual(compare.measures(compare.parse_log(timed))["timing"]["gui_block_max_ms"], "4.0")


if __name__ == "__main__":
    unittest.main()
