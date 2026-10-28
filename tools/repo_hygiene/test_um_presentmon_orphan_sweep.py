"""Tests for UM-PRESENTMON-ORPHAN-SWEEP-1 (round 1): a leftover PresentMon ETW session can never silently starve a capture.

CLASS: before a capture starts, no orphaned PresentMon ETW session can remain on the host; a capture that loses events says so with a typed token; the sweep never touches a
live capture.
Source: lane-UM-PRESENTMON-NOCSV-DIAG-1 r1 (Ultra-Magnus, 2026-10-03). An orphaned default-named `PresentMon` real-time ETW session (left by an earlier killed PresentMon) made
every OTHER-named PresentMon session lose all of its events (15-17k "ETW events were lost" per 12 s), so no CSV was ever written; UM-PRESENTMON-STOP-1 gave each job a unique
--session_name, so the job's own --stop_existing_session no longer cleared the orphan. The same diagnostic inferred that a kill_fallback leaves the job's own MLVAttr3-* session
orphaned too.

  1. Start-PresentMonCapture sweeps, before the spawn, the default `PresentMon` session and every `MLVAttr3-*` session that `logman query -ets` lists -- only when NO live PresentMon
     capture is on the host (a live one may own a capture; the sweep then records that it did not run). "Live" is every `PresentMon*` process except one positively identified as the
     owner's service (UM-SWEEP-OWNER-PRESENTMONSERVICE-1: a registered Windows service's pid) or a readable-path non-capture image; an unidentified one is live. Each termination goes through the pinned PresentMon's own
     --terminate_existing_session (the job's existing helper); `logman stop <name> -ets` runs only for a session still listed after that, and what remains is recorded.
     Every action and exit code lands in presentmon-capture.json (orphanSweep) and the trace;
  2. a job-issued Kill() is followed by the terminate of the job's own named session, in both stop paths, so a killed capture cannot orphan it;
  3. PresentMon's stderr saying "ETW events were lost" is a typed PRESENTMON_EVENTS_LOST detail in presentmon-capture.json and in the PresentMon reason of the failure terminals.
  4. BACHELOR-CUDA-PRESENTMON-CSV-MISSING-1 (Bachelor, 2026-10-27): a live PresentMon* that is no capture image (Intel's PresentMonService, a child of IntelGraphicsSoftware.Service.exe, no
     registered service, path unreadable) skipped the WHOLE sweep on every leg, so the MLVAttr3-* session a lease tree-kill left at 20:06:20Z starved 16 of 16 later cuda captures. Only a capture
     image can own an MLVAttr3-* session: with no capture image alive the sweep runs in scope harness_sessions (MLVAttr3-* only; the default session is left and recorded in `deferred`), and its
     re-checks stop only for a capture image. A sweep that did not leave the host clean is a typed PRESENTMON_ORPHAN_SWEEP_INCOMPLETE detail on the display-report failure reason.
     r2 (Dot advisory on #707, hub-confirmed): "no capture image" was a NAME test, so a second harness's live PresentMon-custom (a permitted -PresentMonName) was no blocker and the sweep stopped
     its live MLVAttr3-* capture. A live process now leaves the harness_sessions blockers only on POSITIVE identity (a registered service, or image PresentMonService under
     IntelGraphicsSoftware.Service.exe per Win32_Process); a failed parent read, a readable path alone or any other name is a blocker. newlyListed compares with the FULL initial listing.

The job's text is EXECUTED (real slices of the generator's template run in pwsh against stubs: a .cmd PresentMon, function stubs for Get-Process and logman); the module's own
functions are called directly. Every item has a mutation test.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import GENERATOR, OWNER_CLIP, PWSH, lf, requires_windows_pwsh, run_pwsh
from tools.repo_hygiene.test_dve_leg_terminals import MODULE, TEMPLATE, SliceHarness, _slice, gpu_summary_fn, presentmon_functions, um_run_log, wait_block
from tools.repo_hygiene.test_dve_presentmon_evidence import PARSE_END, PARSE_START, SIX_FRAMES, UM2_GPU_SUMMARY, UM2_REASON, _StopHarness

STUB_EXE = "PresentMon-stub.cmd"
UNREADABLE = "<path getter throws>"
SESSION_THROWS = "<session getter throws>"
JOB_IMAGE = "C:\\pwsh\\pwsh.exe"
NEW_MODULE_FUNCTIONS = ("Get-AttrCudaPresentMonOrphanSessionName", "Get-AttrCudaEtsSessionListing", "Invoke-AttrCudaLogman", "Stop-AttrCudaEtsSession", "Get-AttrCudaTextEncodingFromHead",
                        "Get-AttrCudaPresentMonEventsLost", "Add-AttrCudaPresentMonEventsLostDetail", "Add-AttrCudaPresentMonEventsLostDetailToReport",
                        "Add-AttrCudaPresentMonOrphanSweepDetailToReport")
OPEN = "UM-PRESENTMON-ORPHAN-SWEEP-1 >>>"
CLOSE = "UM-PRESENTMON-ORPHAN-SWEEP-1 <<<"

# The shape of the real stderr line, from the diagnostic's per-arm stderr files (count only; nothing else of the evidence is reproduced).
LOST_LINE = "warning: 15318 ETW events were lost.\n"

# The byte shape of the REAL PresentMon stderr of the diagnostic's per-arm files (A1-named-dwm.stderr.txt, 862 bytes): UTF-16LE with a FF FE BOM and CRLF line ends. The text is PresentMon's own
# warning wording (no footage, no paths); the same text re-encoded here is byte-for-byte that file (checked once by hand against the diagnostic's artifact, sha256 in the PR body).
REAL_STDERR_TEXT = ("warning: --no_console_stats added because stdout does not support statistics reporting.\r\n"
                    "warning: PresentMon requires elevated privilege in order to query processes that are\r\n"
                    "         short-running or started on another account.  Without it, those processes will\r\n"
                    "         be listed as '<unknown>' and they can't be targeted by --process_name nor trigger\r\n"
                    "         --terminate_on_proc_exit.\r\n"
                    "warning: 15318 ETW events were lost.\r\n")
REAL_STDERR_UTF16LE = REAL_STDERR_TEXT.encode("utf-16")  # Python's "utf-16" on little-endian hosts writes the FF FE BOM

LOGMAN_HEADER = ("Data Collector Set                      Type                          Status\n"
                 "-------------------------------------------------------------------------------\n")
LOGMAN_FOOTER = "\nThe command completed successfully.\n"


def ps(path: Path) -> str:
    return str(path).replace("'", "''")


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def listing(*names: str) -> str:
    # the real layout (UM, 2026-10-03, 00-logman-ets-start.txt): a name shorter than the 40-column field is padded to it; a wider one is printed in full and followed by ONE space
    return LOGMAN_HEADER + "".join(f"{n.ljust(40) if len(n) < 40 else n + ' '}Trace                         Running\n" for n in names) + LOGMAN_FOOTER


def write_logman_stub(d: Path, ps_body: str) -> Path:
    """An executable `logman` for the wrappers (they run a bounded child process, not a function): a .cmd shim around a pwsh stub script that gets logman's arguments in $args."""
    (d / "logman-stub.ps1").write_text(ps_body, encoding="utf-8")
    shim = d / "logman-stub.cmd"
    shim.write_text(f'@echo off\r\n"{PWSH}" -NoLogo -NoProfile -NonInteractive -File "{d / "logman-stub.ps1"}" %*\r\nexit /b %ERRORLEVEL%\r\n', encoding="ascii")
    return shim


def mutate(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"the mutation anchor must exist exactly once: {old!r}"
    return text.replace(old, new, 1)


# --------------------------------------------------------------------------------------------------------------------- module functions
@requires_windows_pwsh
class ModuleFunctionTests(unittest.TestCase):
    """Items 1 and 3, the module's own functions, called directly (the job embeds them verbatim)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="um-orphan-mod-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def call(self, body: str, module_text: str | None = None) -> dict:
        module = MODULE
        if module_text is not None:
            module = self.tmp / "AttrCudaArtifacts.psm1"
            module.write_text(module_text, encoding="utf-8")
        proc = run_pwsh(["-Command", f"$ErrorActionPreference = 'Stop'\nImport-Module '{ps(module)}' -Force\n{body}"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        line = next(l for l in proc.stdout.splitlines() if l.startswith("JSON="))
        return json.loads(line[len("JSON="):])

    def names(self, text: str, module_text: str | None = None) -> list[str]:
        src = self.tmp / "listing.txt"
        src.write_text(text, encoding="utf-8")
        got = self.call(f"$r = @(Get-AttrCudaPresentMonOrphanSessionName -ListingText ([IO.File]::ReadAllText('{ps(src)}')))\nWrite-Output ('JSON=' + (ConvertTo-Json -InputObject $r -Compress))", module_text)
        return got

    def test_the_default_session_and_every_mlvattr3_session_are_the_candidates(self) -> None:
        got = self.names(listing("PresentMon", "MLVAttr3-playback-attr-3-cuda-abc-20261002-163000", "mlvattr3-lower-case-job", "Circular Kernel Context Logger", "EventLog-System"))
        self.assertEqual(got, ["PresentMon", "MLVAttr3-playback-attr-3-cuda-abc-20261002-163000", "mlvattr3-lower-case-job"])

    def test_an_unrelated_session_is_never_a_candidate(self) -> None:
        got = self.names(listing("PresentMonitor2", "PresentMon_other", "NT Kernel Logger", "MyMLVAttr3-x", "SomeoneElsesCapture"))
        self.assertEqual(got, [])

    def test_the_listing_boilerplate_is_never_a_candidate(self) -> None:
        self.assertEqual(self.names(LOGMAN_HEADER + LOGMAN_FOOTER), [])
        self.assertEqual(self.names(""), [])

    # --- UM-SWEEP-WIDE-NAME-PIN-1: real logman prints a name wider than its 40-column field in full, then ONE space (00-logman-ets-start.txt of the diagnostic); every real MLVAttr3-<jobId> is that wide
    WIDE_JOB_SESSION = "MLVAttr3-playback-attr-3-cuda-ultra-magnus-20261003T041500Z-0123456789abcdef"

    def test_a_wide_session_name_followed_by_one_space_is_a_candidate(self) -> None:
        self.assertGreater(len(self.WIDE_JOB_SESSION), 40)
        text = listing("PresentMon", self.WIDE_JOB_SESSION)
        self.assertIn(self.WIDE_JOB_SESSION + " Trace", text, "the fixture is the real layout: one space after a wide name")
        self.assertEqual(self.names(text), ["PresentMon", self.WIDE_JOB_SESSION])

    def test_mutation_a_two_space_separator_would_miss_every_real_wide_orphan(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        anchor = "(PresentMon|MLVAttr3-[A-Za-z0-9_-]+)\\s+\\S"
        self.assertEqual(module.count(anchor), 1, "the mutation anchor must exist exactly once")
        mutated = module.replace(anchor, "(PresentMon|MLVAttr3-[A-Za-z0-9_-]+)\\s{2,}\\S", 1)
        self.assertEqual(self.names(listing(self.WIDE_JOB_SESSION), module_text=mutated), [], "the mutation loses the orphan")
        self.assertEqual(self.names(listing(self.WIDE_JOB_SESSION)), [self.WIDE_JOB_SESSION])

    def test_a_session_is_listed_once(self) -> None:
        self.assertEqual(self.names(listing("PresentMon", "PresentMon")), ["PresentMon"])

    def test_mutation_a_looser_match_would_sweep_a_stranger(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        anchor = "(PresentMon|MLVAttr3-[A-Za-z0-9_-]+)\\s+\\S"
        self.assertEqual(module.count(anchor), 1, "the mutation anchor must exist exactly once")
        mutated = module.replace(anchor, "(PresentMon\\w*|MLVAttr3-[A-Za-z0-9_-]+)\\s+\\S", 1)
        self.assertEqual(self.names(listing("PresentMonitor2"), module_text=mutated), ["PresentMonitor2"])

    def test_mutation_a_stop_wrapper_without_its_name_guard_would_stop_a_stranger(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        anchor = "-notmatch '^(?i:PresentMon|MLVAttr3-[A-Za-z0-9_-]+)$'"
        self.assertEqual(module.count(anchor), 1, "the mutation anchor must exist exactly once")
        marker = self.tmp / "stops.log"
        stub = write_logman_stub(self.tmp, f"Add-Content -LiteralPath '{ps(marker)}' -Value ($args[1])\nexit 0\n")
        got = self.call(f"$r = Stop-AttrCudaEtsSession -SessionName 'NT Kernel Logger' -LogmanPath '{ps(stub)}'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))",
                        module_text=module.replace(anchor, "-notmatch '.'", 1))
        self.assertEqual(got["exitCode"], 0)
        self.assertIn("NT Kernel Logger", marker.read_text(encoding="utf-8"), "the guard is what keeps the wrapper off other people's sessions")

    def lost(self, stderr: bytes | None, module_text: str | None = None) -> dict:
        path = self.tmp / "stderr.txt"
        if stderr is not None:
            path.write_bytes(stderr)
        return self.call(f"$r = Get-AttrCudaPresentMonEventsLost -StderrPath '{ps(path)}'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Depth 4 -Compress))", module_text)

    # --- r2 (sol blocker 3): the REAL stderr is UTF-16LE with a BOM -- PresentMon's stderr redirected by Start-Process under Windows PowerShell's console codepage
    def test_the_real_um_stderr_shape_utf16le_with_a_bom_is_a_typed_events_lost_detail(self) -> None:
        self.assertEqual(REAL_STDERR_UTF16LE[:2], b"\xff\xfe")
        got = self.lost(REAL_STDERR_UTF16LE)
        self.assertTrue(got["detected"], got)
        self.assertEqual((got["messages"], got["maxReported"]), (1, 15318))
        self.assertTrue(got["detail"].startswith("PRESENTMON_EVENTS_LOST"), got["detail"])

    def test_utf16le_without_a_bom_is_decoded_too(self) -> None:
        got = self.lost(REAL_STDERR_TEXT.encode("utf-16-le"))
        self.assertEqual((got["detected"], got["maxReported"]), (True, 15318))

    def test_utf16be_with_a_bom_is_decoded_too(self) -> None:
        got = self.lost(b"\xfe\xff" + REAL_STDERR_TEXT.encode("utf-16-be"))
        self.assertEqual((got["detected"], got["maxReported"]), (True, 15318))

    def test_utf8_with_a_bom_is_decoded_too(self) -> None:
        got = self.lost(b"\xef\xbb\xbf" + REAL_STDERR_TEXT.encode("utf-8"))
        self.assertEqual((got["detected"], got["maxReported"]), (True, 15318))

    def test_an_ascii_stderr_is_decoded(self) -> None:
        got = self.lost(REAL_STDERR_TEXT.encode("ascii"))
        self.assertEqual((got["detected"], got["maxReported"]), (True, 15318))

    def test_a_clean_utf16le_stderr_detects_nothing(self) -> None:
        self.assertFalse(self.lost("warning: --no_console_stats added because stdout does not support statistics reporting.\r\n".encode("utf-16"))["detected"])

    @staticmethod
    def big_utf16le_with_a_stray_byte() -> bytes:
        # the read keeps only the last 256 KB. A stream cut inside a code unit (a write in flight) has an odd length, so the tail's start falls on an odd byte unless it is aligned: every character
        # after it would shift by one byte and read as noise
        padding = ("x" * 99 + "\r\n") * 4000
        data = (padding + REAL_STDERR_TEXT).encode("utf-16") + b"\x0d"
        assert len(data) > 262144 + 1000 and len(data) % 2 == 1 and (len(data) - 262144) % 2 == 1
        return data

    def test_a_utf16le_stream_bigger_than_the_bound_is_read_from_a_code_unit_boundary(self) -> None:
        got = self.lost(self.big_utf16le_with_a_stray_byte())
        self.assertEqual((got["detected"], got["maxReported"]), (True, 15318))

    def test_a_big_clean_utf16le_stream_detects_nothing(self) -> None:
        self.assertFalse(self.lost((("x" * 99 + "\r\n") * 4000).encode("utf-16") + b"\x0d")["detected"])

    def test_mutation_utf8_only_decoding_misses_the_real_stderr_again(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        anchor = "$text = $encoding.encoding.GetString($buffer, 0, $read)"
        self.assertEqual(module.count(anchor), 1, "the mutation anchor must exist exactly once")
        mutated = module.replace(anchor, "$text = [Text.UTF8Encoding]::new($false).GetString($buffer, 0, $read)", 1)
        self.assertFalse(self.lost(REAL_STDERR_UTF16LE, module_text=mutated)["detected"], "the UM state of 2026-10-03: the observed failure carried no PRESENTMON_EVENTS_LOST detail")

    def test_mutation_an_unaligned_tail_reads_as_noise(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        anchor = "$start += ($encoding.codeUnitBytes - ($start % $encoding.codeUnitBytes))"
        self.assertEqual(module.count(anchor), 1, "the mutation anchor must exist exactly once")
        self.assertFalse(self.lost(self.big_utf16le_with_a_stray_byte(), module_text=module.replace(anchor, "$null = $start", 1))["detected"])

    def test_the_um_stderr_line_is_a_typed_events_lost_detail(self) -> None:
        got = self.lost(LOST_LINE.encode("ascii"))
        self.assertTrue(got["detected"])
        self.assertEqual((got["messages"], got["maxReported"]), (1, 15318))
        self.assertTrue(got["detail"].startswith("PRESENTMON_EVENTS_LOST"), got["detail"])
        self.assertIn("15318", got["detail"])

    def test_repeated_lines_report_the_message_count_and_the_largest_count(self) -> None:
        got = self.lost(b"warning: 200 ETW events were lost.\nsomething else\nwarning: 17433 ETW events were lost.\nwarning: 90 ETW events were lost.\n")
        self.assertEqual((got["detected"], got["messages"], got["maxReported"]), (True, 3, 17433))

    def test_a_clean_stderr_detects_nothing(self) -> None:
        got = self.lost(b"")
        self.assertEqual((got["detected"], got["messages"], got["maxReported"], got["detail"]), (False, 0, None, None))
        got = self.lost(b"Started recording.\nStopped recording.\n")
        self.assertFalse(got["detected"])

    def test_a_missing_stderr_detects_nothing_and_never_throws(self) -> None:
        got = self.lost(None)
        self.assertFalse(got["detected"])

    def test_a_loss_older_than_the_bound_is_not_mistaken_for_the_tail(self) -> None:
        # the read is bounded to the stream's tail (like the publish); a loss inside the tail is found, one before it is not claimed
        got = self.lost(b"x" * 400000 + LOST_LINE.encode("ascii"))
        self.assertTrue(got["detected"])

    def test_mutation_a_changed_pattern_loses_the_detail_again(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        self.assertEqual(module.count("ETW events were lost"), 1, "the mutation anchor must exist exactly once")
        mutated = module.replace("ETW events were lost", "ETW events were found", 1)
        self.assertFalse(self.lost(LOST_LINE.encode("ascii"), module_text=mutated)["detected"], "the UM state of 2026-10-03: a recurrence reads as 'output does not exist'")

    def with_detail(self, reason: str | None, events: dict | None) -> str:
        ev = "$null" if events is None else "[pscustomobject]@{ detected = $%s; detail = 'PRESENTMON_EVENTS_LOST: 12 lost' }" % ("true" if events["detected"] else "false")
        r = "$null" if reason is None else "'" + reason.replace("'", "''") + "'"
        proc = run_pwsh(["-Command", f"$ErrorActionPreference = 'Stop'\nImport-Module '{ps(MODULE)}' -Force\n$r = Add-AttrCudaPresentMonEventsLostDetail -Reason {r} -EventsLost ({ev})\nWrite-Output ('OUT=' + $r)"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return next(l for l in proc.stdout.splitlines() if l.startswith("OUT="))[4:]

    def test_the_reason_gains_the_typed_detail_when_events_were_lost(self) -> None:
        out = self.with_detail("PresentMon output does not exist: X", {"detected": True})
        self.assertTrue(out.startswith("PresentMon output does not exist: X"), out)
        self.assertIn("PRESENTMON_EVENTS_LOST: 12 lost", out)

    def test_the_reason_is_unchanged_without_a_loss_or_without_evidence(self) -> None:
        self.assertEqual(self.with_detail("PresentMon output does not exist: X", {"detected": False}), "PresentMon output does not exist: X")
        self.assertEqual(self.with_detail("PresentMon output does not exist: X", None), "PresentMon output does not exist: X")

    def test_a_report_gains_the_detail_in_a_copy_that_keeps_every_other_property_in_order(self) -> None:
        got = self.call(
            "$report = [pscustomobject]@{ status = 'PRESENTMON_UNAVAILABLE'; reason = 'PresentMon output does not exist: X'; chains = @(1, 2); selectedChain = $null }\n"
            "$e = [pscustomobject]@{ detected = $true; detail = 'PRESENTMON_EVENTS_LOST: 12 lost' }\n"
            "$r = Add-AttrCudaPresentMonEventsLostDetailToReport -Report $report -EventsLost $e\n"
            "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ names = @($r.PSObject.Properties.Name); reason = $r.reason; status = $r.status; chains = @($r.chains); same = [object]::ReferenceEquals($r, $report); original = $report.reason }) -Compress))")
        self.assertEqual(got["names"], ["status", "reason", "chains", "selectedChain"])
        self.assertIn("PRESENTMON_EVENTS_LOST", got["reason"])
        self.assertTrue(got["reason"].startswith("PresentMon output does not exist: X"))
        self.assertEqual((got["status"], got["chains"], got["same"]), ("PRESENTMON_UNAVAILABLE", [1, 2], False))
        self.assertEqual(got["original"], "PresentMon output does not exist: X", "the input report is never mutated")

    # --- BACHELOR-CUDA-PRESENTMON-CSV-MISSING-1: a sweep that did not leave the host clean is a typed detail on the no-CSV terminal, never a silent "output does not exist"
    def sweep_detail(self, sweep_ps: str) -> dict:
        return self.call(
            "$report = [pscustomobject]@{ status = 'PRESENTMON_UNAVAILABLE'; reason = 'PresentMon output does not exist: X'; chains = @() }\n"
            f"$s = {sweep_ps}\n"
            "$r = Add-AttrCudaPresentMonOrphanSweepDetailToReport -Report $report -Sweep $s\n"
            "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ names = @($r.PSObject.Properties.Name); reason = $r.reason; same = [object]::ReferenceEquals($r, $report); original = $report.reason }) -Compress))")

    def test_a_skipped_sweep_is_a_typed_detail_on_the_reason(self) -> None:
        got = self.sweep_detail("[ordered]@{ ran = $false; skippedReason = 'presentmon_process_alive'; livePresentMonProcessIds = @(43684); remaining = @(); deferred = @() }")
        self.assertEqual(got["reason"], "PresentMon output does not exist: X [PRESENTMON_ORPHAN_SWEEP_INCOMPLETE: skipped=presentmon_process_alive livePids=43684]")
        self.assertEqual((got["names"], got["same"], got["original"]), (["status", "reason", "chains"], False, "PresentMon output does not exist: X"))

    def test_a_left_session_is_named_in_the_detail(self) -> None:
        got = self.sweep_detail("[ordered]@{ ran = $true; skippedReason = $null; abortedReason = 'presentmon_process_started_during_sweep'; remaining = @('MLVAttr3-b'); deferred = @('PresentMon'); livePresentMonProcessIds = @(7001) }")
        self.assertIn("PRESENTMON_ORPHAN_SWEEP_INCOMPLETE:", got["reason"])
        for part in ("aborted=presentmon_process_started_during_sweep", "remaining=MLVAttr3-b", "deferred=PresentMon", "livePids=7001"):
            self.assertIn(part, got["reason"])

    def test_a2_a_session_that_appeared_after_the_scan_is_named_in_the_detail(self) -> None:
        # adversary A2: nothing skipped, aborted, remaining or deferred, but the final listing holds a session the first did not -- the host was never verified clean
        got = self.sweep_detail("[ordered]@{ ran = $true; skippedReason = $null; abortedReason = $null; remaining = @(); deferred = @(); newlyListed = @('MLVAttr3-other-killed-job'); listError = $null; error = $null; livePresentMonProcessIds = @(7001) }")
        self.assertEqual(got["reason"], "PresentMon output does not exist: X [PRESENTMON_ORPHAN_SWEEP_INCOMPLETE: newlyListed=MLVAttr3-other-killed-job livePids=7001]")

    def test_a_sweep_that_left_the_host_clean_adds_nothing(self) -> None:
        for sweep in ("[ordered]@{ ran = $true; skippedReason = $null; abortedReason = $null; remaining = @(); deferred = @(); listError = $null; error = $null }", "$null"):
            got = self.sweep_detail(sweep)
            self.assertTrue(got["same"], sweep)
            self.assertEqual(got["reason"], "PresentMon output does not exist: X")

    def test_a_reason_that_already_carries_the_token_is_never_given_it_twice(self) -> None:
        got = self.call(
            "$report = [pscustomobject]@{ status = 'PRESENTMON_UNAVAILABLE'; reason = 'x [PRESENTMON_ORPHAN_SWEEP_INCOMPLETE: skipped=a]' }\n"
            "$r = Add-AttrCudaPresentMonOrphanSweepDetailToReport -Report $report -Sweep ([ordered]@{ ran = $false; skippedReason = 'b' })\n"
            "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ same = [object]::ReferenceEquals($r, $report) }) -Compress))")
        self.assertTrue(got["same"])

    def test_a_report_with_nothing_to_add_comes_back_as_the_same_object(self) -> None:
        got = self.call(
            "$report = [pscustomobject]@{ status = 'DISPLAY_ASLEEP'; reason = 'r' }\n"
            "$e = [pscustomobject]@{ detected = $false; detail = $null }\n"
            "$r = Add-AttrCudaPresentMonEventsLostDetailToReport -Report $report -EventsLost $e\n"
            "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ same = [object]::ReferenceEquals($r, $report) }) -Compress))")
        self.assertTrue(got["same"])

    # --- r3 (sol B1 on 2519ce4f, report-copy-shape): both report helpers accept [object]; a dictionary report used to lose status and chains (PSObject.Properties of a dictionary are its
    # own members, not its keys). Every accepted shape now comes back as the same pscustomobject: the report's fields, only `reason` changed.
    SHAPES = {"pscustomobject": "[pscustomobject]@{ status = 'PRESENTMON_UNAVAILABLE'; reason = 'r'; chains = @(1, 2) }",
              "ordered": "[ordered]@{ status = 'PRESENTMON_UNAVAILABLE'; reason = 'r'; chains = @(1, 2) }",
              "hashtable": "@{ status = 'PRESENTMON_UNAVAILABLE'; reason = 'r'; chains = @(1, 2) }"}
    HELPERS = {"sweep": "Add-AttrCudaPresentMonOrphanSweepDetailToReport -Report $report -Sweep @{ skippedReason = 'presentmon_process_alive' }",
               "events": "Add-AttrCudaPresentMonEventsLostDetailToReport -Report $report -EventsLost ([pscustomobject]@{ detected = $true; detail = 'PRESENTMON_EVENTS_LOST: 12 lost' })"}

    def shaped(self, shape: str, helper: str, module_text: str | None = None) -> dict:
        return self.call(f"$report = {self.SHAPES[shape]}\n$r = {self.HELPERS[helper]}\n"
                         "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ type = $r.GetType().Name; names = @(@($r.PSObject.Properties.Name) | Sort-Object); "
                         "status = $r.status; reason = $r.reason; chains = @($r.chains); original = $report.reason }) -Compress))", module_text)

    def test_r3_b1_every_report_shape_keeps_its_fields_and_changes_only_the_reason(self) -> None:
        for helper in self.HELPERS:
            outputs = {shape: self.shaped(shape, helper) for shape in self.SHAPES}
            for shape, got in outputs.items():
                self.assertEqual((got["type"], got["names"], got["status"], got["chains"]), ("PSCustomObject", ["chains", "reason", "status"], "PRESENTMON_UNAVAILABLE", [1, 2]), (helper, shape))
                self.assertTrue(got["reason"].startswith("r ["), (helper, shape, got["reason"]))
                self.assertEqual(got["original"], "r", "the input report is never mutated")
            self.assertEqual(outputs["ordered"], outputs["pscustomobject"], helper)
            self.assertEqual(outputs["hashtable"], outputs["pscustomobject"], helper)

    def test_r3_b1_an_ordered_report_keeps_its_key_order(self) -> None:
        got = self.call("$report = [ordered]@{ status = 'S'; reason = 'r'; chains = @(); selectedChain = $null }\n"
                        "$r = Add-AttrCudaPresentMonOrphanSweepDetailToReport -Report $report -Sweep @{ skippedReason = 'x' }\n"
                        "Write-Output ('JSON=' + (ConvertTo-Json -InputObject @($r.PSObject.Properties.Name) -Compress))")
        self.assertEqual(got, ["status", "reason", "chains", "selectedChain"])

    def test_r3_a3_a_dictionary_report_with_a_keys_entry_keeps_every_field(self) -> None:
        # adversary A3 on 3ae8ebb2: `$Report.Keys` on a dictionary holding a 'Keys' entry reads that entry, so chains (and Keys itself) were dropped; PSBase.Keys is the real key collection
        for helper in self.HELPERS:
            got = self.call("$report = [ordered]@{ status = 'PRESENTMON_UNAVAILABLE'; reason = 'r'; chains = @(1, 2); Keys = 'status' }\n"
                            f"$r = {self.HELPERS[helper]}\n"
                            "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ names = @($r.PSObject.Properties.Name); chains = @($r.chains); keys = $r.Keys }) -Compress))")
            self.assertEqual((got["names"], got["chains"], got["keys"]), (["status", "reason", "chains", "Keys"], [1, 2], "status"), helper)

    def test_r3_a2_a_path_that_contains_the_token_name_still_gets_the_detail(self) -> None:
        # adversary A2 on 3ae8ebb2: the already-annotated check matched any substring, so a CSV path under a folder named like the token suppressed the whole detail
        got = self.call("$report = [pscustomobject]@{ status = 'PRESENTMON_UNAVAILABLE'; reason = 'PresentMon output does not exist: C:\\mlvtmp\\PRESENTMON_ORPHAN_SWEEP_INCOMPLETE\\run\\presentmon.csv' }\n"
                        "$r = Add-AttrCudaPresentMonOrphanSweepDetailToReport -Report $report -Sweep @{ ran = $false; skippedReason = 'presentmon_process_alive'; livePresentMonProcessIds = @(6100) }\n"
                        "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ reason = $r.reason }) -Compress))")
        self.assertTrue(got["reason"].endswith("[PRESENTMON_ORPHAN_SWEEP_INCOMPLETE: skipped=presentmon_process_alive livePids=6100]"), got["reason"])

    def test_r3_a2_a_bare_detail_reason_is_still_never_annotated_twice(self) -> None:
        got = self.call("$report = [pscustomobject]@{ reason = 'PRESENTMON_ORPHAN_SWEEP_INCOMPLETE: skipped=a' }\n"
                        "$r = Add-AttrCudaPresentMonOrphanSweepDetailToReport -Report $report -Sweep @{ ran = $false; skippedReason = 'b' }\n"
                        "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ same = [object]::ReferenceEquals($r, $report) }) -Compress))")
        self.assertTrue(got["same"])

    def test_mutation_the_property_only_copy_loses_a_dictionary_reports_fields_again(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        branch = ("    if ($Report -is [System.Collections.IDictionary]) { foreach ($key in @($Report.PSBase.Keys)) { $copy[[string]$key] = $Report[$key] } }\n"
                  "    else { foreach ($property in $Report.PSObject.Properties) { $copy[$property.Name] = $property.Value } }\n")
        self.assertEqual(module.count(branch), 2, "both report helpers carry the dictionary branch")
        mutated = module.replace(branch, "    foreach ($property in $Report.PSObject.Properties) { $copy[$property.Name] = $property.Value }\n", 2)
        for helper in self.HELPERS:
            got = self.shaped("ordered", helper, module_text=mutated)
            self.assertIsNone(got["status"], f"sol's B1 repro ({helper}): status is lost")

    # --- the wrappers run logman as a child process under a deadline (UM-SWEEP-LOGMAN-BOUND-1); the tests hand them a stub executable
    HANGING_STUB = "Start-Sleep -Seconds 6\nexit 0\n"

    def test_the_listing_wrapper_returns_the_text_and_the_exit_code_of_logman_query(self) -> None:
        stub = write_logman_stub(self.tmp, "if ($args[0] -eq 'query' -and $args[1] -eq '-ets') { Write-Output 'PresentMon   Trace   Running' }\nexit 0\n")
        got = self.call(f"$r = Get-AttrCudaEtsSessionListing -LogmanPath '{ps(stub)}'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))")
        self.assertEqual((got["exitCode"], got["error"], got["timedOut"]), (0, None, False))
        self.assertIn("PresentMon   Trace   Running", got["text"])

    def test_the_listing_wrapper_reports_a_failing_logman_without_throwing(self) -> None:
        stub = write_logman_stub(self.tmp, "Write-Output 'ERROR'\nexit 5\n")
        got = self.call(f"$r = Get-AttrCudaEtsSessionListing -LogmanPath '{ps(stub)}'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))")
        self.assertEqual(got["exitCode"], 5)

    def test_the_listing_wrapper_reports_a_logman_that_cannot_run_without_throwing(self) -> None:
        got = self.call("$r = Get-AttrCudaEtsSessionListing -LogmanPath 'C:\\no-such-dir\\logman.exe'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))")
        self.assertIsNone(got["exitCode"])
        self.assertTrue(got["error"], got)

    def timed(self, call: str, module_text: str | None = None) -> dict:
        return self.call("$sw = [Diagnostics.Stopwatch]::StartNew()\n" + call + "\n$sw.Stop()\n"
                         "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ result = $r; seconds = $sw.Elapsed.TotalSeconds }) -Depth 4 -Compress))", module_text)

    def test_a_listing_that_stalls_is_cut_off_at_its_deadline(self) -> None:
        stub = write_logman_stub(self.tmp, self.HANGING_STUB)
        got = self.timed(f"$r = Get-AttrCudaEtsSessionListing -LogmanPath '{ps(stub)}' -TimeoutSeconds 1")
        self.assertTrue(got["result"]["timedOut"], got)
        self.assertIsNone(got["result"]["exitCode"])
        self.assertIn("did not exit within 1 s", got["result"]["error"])
        self.assertLess(got["seconds"], 5.0, "cut off at the deadline, not after the stall")

    def test_a_stop_that_stalls_is_cut_off_at_its_deadline(self) -> None:
        stub = write_logman_stub(self.tmp, self.HANGING_STUB)
        got = self.timed(f"$r = Stop-AttrCudaEtsSession -SessionName 'MLVAttr3-job-1' -LogmanPath '{ps(stub)}' -TimeoutSeconds 1")
        self.assertTrue(got["result"]["timedOut"], got)
        self.assertIsNone(got["result"]["exitCode"])
        self.assertLess(got["seconds"], 5.0)

    def test_mutation_without_the_deadline_a_stalled_logman_holds_the_caller(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        anchor = "$child.WaitForExit($TimeoutSeconds * 1000)"
        self.assertEqual(module.count(anchor), 1, "the mutation anchor must exist exactly once")
        stub = write_logman_stub(self.tmp, self.HANGING_STUB)
        got = self.timed(f"$r = Get-AttrCudaEtsSessionListing -LogmanPath '{ps(stub)}' -TimeoutSeconds 1", module_text=module.replace(anchor, "$child.WaitForExit(600000)", 1))
        self.assertFalse(got["result"]["timedOut"], "the mutation never times out")
        self.assertEqual(got["result"]["exitCode"], 0)
        self.assertGreater(got["seconds"], 5.0, "and holds the caller for the whole stall: the bound is what prevents it")

    def test_the_stop_wrapper_stops_only_this_harnesss_own_session_names(self) -> None:
        marker = self.tmp / "stops.log"
        stub = write_logman_stub(self.tmp, f"if ($args[0] -eq 'stop') {{ Add-Content -LiteralPath '{ps(marker)}' -Value ($args[1] + ' ' + $args[2]); exit 0 }} else {{ exit 9 }}\n")
        got = self.call(
                        "$all = foreach ($n in @('PresentMon', 'presentmon', 'MLVAttr3-job-1', 'NT Kernel Logger', 'PresentMon_other', 'MLVAttr3-', 'EventLog-System', 'x -ets; y', 'MyMLVAttr3-x')) "
                        f"{{ [ordered]@{{ name = $n; result = (Stop-AttrCudaEtsSession -SessionName $n -LogmanPath '{ps(stub)}') }} }}\n"
                        "Write-Output ('JSON=' + (ConvertTo-Json -InputObject @($all) -Depth 4 -Compress))")
        by_name = {row["name"]: row["result"] for row in got}
        for ok in ("PresentMon", "presentmon", "MLVAttr3-job-1"):
            self.assertEqual((by_name[ok]["exitCode"], by_name[ok]["error"]), (0, None), ok)
        for refused in ("NT Kernel Logger", "PresentMon_other", "MLVAttr3-", "EventLog-System", "x -ets; y", "MyMLVAttr3-x"):
            self.assertIsNone(by_name[refused]["exitCode"], refused)
            self.assertIn("refused", by_name[refused]["error"], refused)
        self.assertEqual(sorted(marker.read_text(encoding="utf-8").split("\n")[:-1]), ["MLVAttr3-job-1 -ets", "PresentMon -ets", "presentmon -ets"])

    def test_the_detail_is_never_added_twice(self) -> None:
        once = self.with_detail("r", {"detected": True})
        again = run_pwsh(["-Command", f"$ErrorActionPreference = 'Stop'\nImport-Module '{ps(MODULE)}' -Force\n"
                          f"$e = [pscustomobject]@{{ detected = $true; detail = 'PRESENTMON_EVENTS_LOST: 12 lost' }}\n"
                          f"Write-Output ('OUT=' + (Add-AttrCudaPresentMonEventsLostDetail -Reason '{once}' -EventsLost $e))"])
        self.assertEqual(next(l for l in again.stdout.splitlines() if l.startswith("OUT="))[4:], once)


# --------------------------------------------------------------------------------------------------------------------- the sweep in Start-PresentMonCapture
@requires_windows_pwsh
class SweepExecutedTests(SliceHarness, unittest.TestCase):
    """Item 1: the REAL Start-PresentMonCapture and Invoke-PresentMonOrphanSweep, run against a .cmd PresentMon (it logs its argv and, for --terminate_existing_session, records
    the termination), a Get-Process stub (the alive PresentMon pids) and a logman stub (a session list that the terminations edit)."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def run_start(self, name: str, sessions: tuple[str, ...] = ("PresentMon", "MLVAttr3-old-killed-job", "Circular Kernel Context Logger"), live_pids: tuple[int, ...] = (),
                  helper_works: bool = True, logman_stop_works: bool = True, enabled: bool = True, get_process_throws: bool = False, logman_query_fails: bool = False, live_from_second_check: bool = False,
                  text: str | None = None, extra_from_query: int = 0, live_after_terminate_of: str | None = None, query_exits: dict[int, int] | None = None,
                  logman_hangs: bool = False, logman_stop_hangs: bool = False, logman_timeout_seconds: int = 0,
                  others: tuple[tuple[int, str, str | None], ...] = (), services: tuple[tuple[int, str], ...] = (), service_lookup_fails: bool = False,
                  processes: tuple[tuple[int, str, int, int | None], ...] = (), process_lookup_fails: bool = False,
                  go_live_as: tuple[int, str, str | None] | None = None, command_lines: tuple[tuple[int | str, str], ...] = (), command_line_query_fails_from: int = 0,
                  go_live_command_line: str | None = None, pm_name: str = STUB_EXE, presentmon_sha: str | None = None,
                  pinned_copies: tuple[Path, ...] = (), session_ids: dict[int, int | str | None] | None = None,
                  job_session: int | str | None = None) -> tuple[int, dict, Path, str]:
        # r4: `session_ids` gives a process row (others / go_live_as, by pid) its SessionId: an int, None (a null value) or SESSION_THROWS (the getter throws); a pid not in it has none at all.
        # `job_session` adds the sweeping job's own row ($PID) to Get-Process with that SessionId (None: no row, as before -- the job's session is then unknown)
        # r2: `processes` are the Win32_Process rows (id, name, parent id, creation minute or None for an unreadable CreationDate) the parent read sees; `go_live_as` is the process that appears
        # once live_after_terminate_of is terminated (default: the pinned image, pid 5151)
        # r3: `command_lines` are the rows (pid, CommandLine) of the snapshot's ONE unfiltered Win32_Process query (a process without a row reads as an unreadable command line);
        # `go_live_command_line` is the go-live process's row once it appears; command_line_query_fails_from=N makes that query fail from its Nth call on (0: never); `pm_name` is the
        # configured -PresentMonName (the stub executable's name in the cache); `presentmon_sha` is the job's pinned $PresentMonSha (None: unset; "PINNED": the stub's own SHA-256);
        # `pinned_copies` are byte copies of the stub written elsewhere (another harness's renamed copy of the pinned binary)
        d = self.stmp / name
        (d / "cache").mkdir(parents=True)
        (d / "legOut").mkdir()
        (d / "sessions.txt").write_text("\n".join(sessions) + "\n", encoding="utf-8")
        (d / "terminated.txt").write_text("", encoding="utf-8")
        (d / "stopped.txt").write_text("", encoding="utf-8")
        (d / "calls.log").write_text("", encoding="utf-8")
        record = f"echo %2>>\"{d / 'terminated.txt'}\"\r\n" if helper_works else ""
        # a PresentMon process that appears once the helper has been asked to terminate this session (another capture starting mid-sweep)
        goes_live = f"if /i \"%2\"==\"{live_after_terminate_of}\" echo x>\"{d / 'go-live.txt'}\"\r\n" if live_after_terminate_of else ""
        (d / "cache" / pm_name).write_text(
            "@echo off\r\n"
            f"echo %*>>\"{d / 'calls.log'}\"\r\n"
            "echo %* | findstr /c:\"--terminate_existing_session\" >nul\r\n"
            "if errorlevel 1 exit /b 0\r\n"
            + goes_live + record + ("exit /b 0\r\n" if helper_works else "exit /b 1\r\n"), encoding="ascii")
        pinned_bytes = (d / "cache" / pm_name).read_bytes()
        for copy in pinned_copies:
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(pinned_bytes)
        if presentmon_sha == "PINNED":
            presentmon_sha = hashlib.sha256(pinned_bytes).hexdigest().upper()
        exits = "; ".join(f"{k} = {v}" for k, v in (query_exits or {}).items())
        logman_stub = write_logman_stub(d, (
            f"$d = '{ps(d)}'\n"
            "$a = @($args)\n"
            "if ($a[0] -eq 'query') {\n"
            + ("    Write-Output 'ERROR: stub logman query failure'; exit 1\n" if logman_query_fails else
               ("    Start-Sleep -Seconds 60\n" if logman_hangs else "")
               + "    Add-Content -LiteralPath (Join-Path $d 'queries.txt') -Value 'q'\n"
               "    $qn = @(Get-Content -LiteralPath (Join-Path $d 'queries.txt')).Count\n"
               f"    $exits = @{{ {exits} }}\n"
               "    if ($exits.ContainsKey($qn)) { Write-Output 'ERROR: stub transient logman failure'; exit $exits[$qn] }\n"
               "    $gone = @(Get-Content -LiteralPath (Join-Path $d 'terminated.txt')) + @(Get-Content -LiteralPath (Join-Path $d 'stopped.txt'))\n"
               "    $live = @(Get-Content -LiteralPath (Join-Path $d 'sessions.txt') | Where-Object { $_ -and ($gone -notcontains $_.Trim()) })\n"
               # another job's capture starting between the scan and a later listing: its session is listed from the Nth query on
               f"    if ({extra_from_query} -gt 0 -and $qn -ge {extra_from_query}) {{ $live += 'MLVAttr3-another-live-job' }}\n"
               f"    $text = \"{LOGMAN_HEADER.replace(chr(10), '`n')}\"\n"
               # the real layout: a name wider than the 40-column field is printed in full, then ONE space
               "    foreach ($n in $live) { $text += ($(if ($n.Length -lt 40) { $n.PadRight(40) } else { $n + ' ' }) + \"Trace                         Running`n\") }\n"
               "    Write-Output ($text + 'The command completed successfully.'); exit 0\n")
            + "}\n"
            "if ($a[0] -eq 'stop') {\n"
            + ("    Start-Sleep -Seconds 60\n" if logman_stop_hangs else "")
            + ("    Add-Content -LiteralPath (Join-Path $d 'stopped.txt') -Value $a[1]; exit 0\n" if logman_stop_works else
               "    Write-Output 'ERROR: stub logman stop failure'; exit 1\n")
            + "}\n"
            "exit 0\n"))
        body = text if text is not None else TEMPLATE
        pids = ", ".join(str(p) for p in live_pids)
        # UM-SWEEP-OWNER-PRESENTMONSERVICE-1: other processes whose name merely starts with PresentMon (the owner's PresentMonService), as (id, name, path-or-None-or-UNREADABLE).
        # UNREADABLE is a real unreadable path: the Path getter throws, as it does for a SYSTEM process read by a non-elevated agent.
        extra = ""
        denied_type = ""

        sessions = dict(session_ids or {})
        if job_session is not None:
            sessions["$PID"] = job_session

        def session_member(i: int | str) -> str:
            if i not in sessions or sessions[i] == SESSION_THROWS:
                return ""
            return f"; SessionId = {('$null' if sessions[i] is None else sessions[i])}"

        def process_row(i: int, n: str, pth: str | None) -> str:
            nonlocal denied_type
            throws = sessions.get(i) == SESSION_THROWS
            if pth == UNREADABLE or throws:
                # a .NET property that throws, like Process.Path on a process the caller may not open (PowerShell reads a throwing getter as $null -- measured on pwsh 7.6 -- which is also what a real unreadable Path yields)
                # r4: MlvDeniedSessionProcess's SessionId getter throws too (the session probe's failure case); MlvDeniedProcess's SessionId is a plain value (null when not given)
                denied_type = ("Add-Type -TypeDefinition 'public class MlvDeniedProcess { public int Id; public string Name; public object SessionId; "
                               "public string Path { get { throw new System.UnauthorizedAccessException(\"Access is denied\"); } } } "
                               "public class MlvDeniedSessionProcess { public int Id; public string Name; "
                               "public string Path { get { throw new System.UnauthorizedAccessException(\"Access is denied\"); } } "
                               "public object SessionId { get { throw new System.UnauthorizedAccessException(\"Access is denied\"); } } }'\n")
                return f"[{'MlvDeniedSessionProcess' if throws else 'MlvDeniedProcess'}]@{{ Id = {i}; Name = '{n}'{session_member(i)} }}"
            return f"[pscustomobject]@{{ Id = {i}; Name = '{n}'; Path = {('$null' if pth is None else ps_quote(pth))}{session_member(i)} }}"

        for i, n, pth in others:
            extra += f"    $all += {process_row(i, n, pth)}\n"
        if job_session is not None:
            job_row = process_row("$PID", "pwsh", JOB_IMAGE)
            extra += f"    $all += {job_row}\n"
        go_live_row = (process_row(*go_live_as) if go_live_as is not None
                       else "[pscustomobject]@{ Id = 5151; Name = [IO.Path]::GetFileNameWithoutExtension($PresentMonName); Path = $null }")
        service_rows = "; ".join(f"@{{ ProcessId = {i}; Name = '{n}' }}" for i, n in services)
        # a creation stamp is a minute offset (a local DateTime), None (unreadable), or an ISO string with an offset (a DateTimeOffset: the instant is fixed whatever the host's zone)
        process_rows = "; ".join(f"@{{ ProcessId = {i}; Name = '{n}'; ParentProcessId = {pp}; CreationMinute = {('$null' if m is None else (ps_quote(m) if isinstance(m, str) else m))} }}" for i, n, pp, m in processes)
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            "function Test-AttrCudaPresentMonTraceReady($Proc) { [pscustomobject]@{ ready = $true; detail = 'stub' } }\n"
            f"$traceFile = '{ps(d / 'trace.log')}'\n"
            "function Write-JobTrace([string]$Message) { Add-Content -LiteralPath $traceFile -Value $Message }\n"
            + denied_type +
            "function Get-Process {\n"
            "    [CmdletBinding()] param([string[]]$Name)\n"
            "    $global:getProcessCalls = 1 + [int]$global:getProcessCalls\n"
            + ("    throw 'stub Get-Process failure'\n" if get_process_throws else
               "    $all = @()\n"
               f"    if (Test-Path -LiteralPath '{ps(d / 'go-live.txt')}') {{ $all += {go_live_row} }}\n"
               + extra
               + ("    if ($global:getProcessCalls -ge 2) {\n" if live_from_second_check else "    if ($true) {\n")
               + f"        foreach ($p in @({pids})) {{ $all += [pscustomobject]@{{ Id = [int]$p; Name = [IO.Path]::GetFileNameWithoutExtension($PresentMonName); Path = $null }} }}\n"
               "    }\n"
               # the real cmdlet honours -Name (a wildcard on the image name): a process whose name does not match is never returned
               "    $all | Where-Object { $p = $_; (-not $Name) -or (@($Name | Where-Object { $p.Name -like $_ }).Count -gt 0) }\n")
            + "}\n"
            # the real cmdlet reads Win32_Service by a WQL filter; a failure is a non-terminating error that -ErrorAction Stop turns into a throw
            "function Get-CimInstance {\n"
            "    [CmdletBinding()] param([string]$ClassName, [string]$Filter, [string[]]$Property)\n"
            "    $global:getCimCalls = 1 + [int]$global:getCimCalls\n"
            # r3: the snapshot's unfiltered command-line query (ProcessId, CommandLine for every process); a failure is the whole query failing, as for an access-denied WMI namespace
            "    if ($ClassName -eq 'Win32_Process' -and -not $Filter) {\n"
            "        $global:commandLineQueries = 1 + [int]$global:commandLineQueries\n"
            f"        if ({command_line_query_fails_from} -gt 0 -and $global:commandLineQueries -ge {command_line_query_fails_from}) {{ Write-Error 'stub Win32_Process command-line query failure'; return }}\n"
            + "".join(f"        [pscustomobject]@{{ ProcessId = {i}; CommandLine = {ps_quote(c)} }}\n" for i, c in command_lines)
            + (f"        if (Test-Path -LiteralPath '{ps(d / 'go-live.txt')}') {{ [pscustomobject]@{{ ProcessId = {(go_live_as or (5151,))[0]}; CommandLine = {ps_quote(go_live_command_line)} }} }}\n" if go_live_command_line else "")
            + "        return\n"
            "    }\n"
            + ("    if ($ClassName -eq 'Win32_Service') { Write-Error 'stub Get-CimInstance failure'; return }\n" if service_lookup_fails else "")
            + ("    if ($ClassName -eq 'Win32_Process') { Write-Error 'stub Win32_Process failure'; return }\n" if process_lookup_fails else "")
            + "    $want = [int]([regex]::Match($Filter, 'ProcessId\\s*=\\s*(\\d+)').Groups[1].Value)\n"
            # r2: the parent read (Win32_Process: ParentProcessId, Name, CreationDate as a DateTime, like the real CIM class)
            f"    if ($ClassName -eq 'Win32_Process') {{\n"
            f"        $processRows = @({process_rows})\n"
            "        foreach ($r in $processRows) { if ($r.ProcessId -eq $want) { [pscustomobject]@{ Name = $r.Name; ProcessId = $r.ProcessId; ParentProcessId = $r.ParentProcessId; "
            "CreationDate = $(if ($null -eq $r.CreationMinute) { $null } elseif ($r.CreationMinute -is [string] -and $r.CreationMinute.StartsWith('local:')) { [DateTimeOffset]::Parse($r.CreationMinute.Substring(6)).LocalDateTime } elseif ($r.CreationMinute -is [string]) { [DateTimeOffset]::Parse($r.CreationMinute) } else { (Get-Date '2026-10-27T00:00:00').AddMinutes([int]$r.CreationMinute) }) } } }\n"
            "        return\n"
            "    }\n"
            + "    if ($ClassName -ne 'Win32_Service') { throw \"the sweep asked CIM for $ClassName\" }\n"
            f"    $services = @({service_rows})\n"
            "    foreach ($s in $services) { if ($s.ProcessId -eq $want) { [pscustomobject]@{ Name = $s.Name; ProcessId = $s.ProcessId } } }\n"
            "}\n"
            # the sweep's wrappers run an executable (bounded by a deadline), so the stub is one: a .cmd shim around a pwsh script
            + f"$presentMonLogmanPath = '{ps(logman_stub)}'\n"
            + (f"$presentMonLogmanTimeoutSeconds = {logman_timeout_seconds}\n" if logman_timeout_seconds else "")
            + f"$Cache ='{ps(d / 'cache')}'\n$PresentMonName = '{pm_name}'\n$ExeName = 'MLVApp.exe'\n$PresentMonTimedSeconds = 60\n"
            + (f"$PresentMonSha = '{presentmon_sha}'\n" if presentmon_sha else "")
            + "$PresentMonSessionName = 'MLVAttr3-the-new-job'\n$PresentMonTerminateOnProcExit = $true\n"
            f"$legOut = '{ps(d / 'legOut')}'\n"
            "$presentMonPath = Join-Path $legOut 'presentmon.csv'\n"
            "$presentMonStdoutPath = Join-Path $legOut 'presentmon-stdout.txt'\n$presentMonStderrPath = Join-Path $legOut 'presentmon-stderr.txt'\n"
            "$presentMonTraceReadiness = [ordered]@{}\n$presentMonStreams = [ordered]@{ csvSeenDuringReadiness = $false }\n"
            + ("$presentMonOrphanSweep = [ordered]@{}\n" if enabled else "")
            + presentmon_functions(body) + "\n"
            "$proc = Start-PresentMonCapture $presentMonPath\n"
            "[void]$proc.WaitForExit(20000)\n"
            "if ($null -ne $presentMonOrphanSweep) { Write-Output ('SWEEP=' + ($presentMonOrphanSweep | ConvertTo-Json -Depth 6 -Compress)) }\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        sweep: dict = {}
        for line in proc.stdout.splitlines():
            if line.startswith("SWEEP="):
                sweep = json.loads(line[len("SWEEP="):])
        return proc.returncode, sweep, d, proc.stdout + proc.stderr

    @staticmethod
    def calls(d: Path) -> list[str]:
        return [l.strip() for l in (d / "calls.log").read_text(encoding="utf-8").splitlines() if l.strip()]

    @staticmethod
    def terminated_names(calls: list[str]) -> list[str]:
        return [m.group(1) for c in calls if (m := re.search(r"--session_name (\S+) --terminate_existing_session", c))]

    def test_orphans_with_no_presentmon_process_alive_are_swept_before_the_spawn(self) -> None:
        code, sweep, d, out = self.run_start("swept")
        self.assertEqual(code, 0, out)
        calls = self.calls(d)
        self.assertEqual(self.terminated_names(calls), ["PresentMon", "MLVAttr3-old-killed-job"], "the default session and the leftover job session, never the unrelated kernel logger")
        spawn = [i for i, c in enumerate(calls) if "--process_name" in c]
        self.assertEqual(len(spawn), 1)
        self.assertTrue(all(i < spawn[0] for i, c in enumerate(calls) if "--terminate_existing_session" in c), "every sweep action precedes the spawn")
        self.assertTrue(sweep["ran"])
        self.assertIsNone(sweep["skippedReason"])
        self.assertEqual(sweep["listed"], ["PresentMon", "MLVAttr3-old-killed-job"])
        self.assertEqual([(a["session"], a["method"], a["exitCode"]) for a in sweep["actions"]], [("PresentMon", "presentmon_terminate", 0), ("MLVAttr3-old-killed-job", "presentmon_terminate", 0)])
        self.assertEqual(sweep["remaining"], [])
        self.assertIsNone(sweep["error"])
        trace = (d / "trace.log").read_text(encoding="utf-8")
        self.assertIn("step presentmon-orphan-sweep", trace)
        self.assertIn("PresentMon", trace)

    def test_a_live_presentmon_process_is_never_swept(self) -> None:
        code, sweep, d, out = self.run_start("live", live_pids=(4242,))
        self.assertEqual(code, 0, out)
        calls = self.calls(d)
        self.assertEqual(self.terminated_names(calls), [], "another capture may own a session: nothing is terminated")
        self.assertEqual(len([c for c in calls if "--process_name" in c]), 1, "the capture itself still starts")
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "", "and logman stop is not used either")
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual(sweep["livePresentMonProcessIds"], [4242])
        self.assertEqual(sweep["actions"], [])
        self.assertIn("presentmon_process_alive", (d / "trace.log").read_text(encoding="utf-8"))

    # --- UM-SWEEP-OWNER-PRESENTMONSERVICE-1: the liveness check is a live CAPTURE-MODE PresentMon (the pinned executable), never a process that merely starts with "PresentMon"
    # r2 (hub ruling on sol blocker 1 / fable): a PresentMon* process is NOT live only when it is POSITIVELY the owner's service (its pid is a registered Windows service's ProcessId) or its path is
    # readable and it is not a capture image; an unreadable path with no service registration is live (the sweep never touches a host it cannot identify)
    SERVICE = (7001, "PresentMonService", "C:\\Program Files\\Intel\\PresentMonService.exe")
    SERVICE_ROW = ((7001, "PresentMonService"),)
    SERVICE_UNREADABLE = (7001, "PresentMonService", UNREADABLE)
    # r2: the Bachelor shape as Win32_Process sees it -- PresentMonService (7001) under IntelGraphicsSoftware.Service.exe (900), the parent started first
    INTEL_TREE = ((7001, "PresentMonService", 900, 10), (900, "IntelGraphicsSoftware.Service.exe", 4, 5))
    INTEL_DETAIL = {"name": "PresentMonService", "pid": 7001, "pathReadable": False, "reason": "unreadable_path_not_a_registered_service",
                    "nonCaptureIdentity": "intel_graphics_presentmonservice", "parentName": "IntelGraphicsSoftware.Service.exe", "parentPid": 900, "identityError": None}
    # Dot's repro (#707): a second harness started with the permitted -PresentMonName PresentMon-custom.exe owns a live MLVAttr3-* capture; the name misses the capture-image pattern
    CUSTOM_UNREADABLE = (6100, "PresentMon-custom", UNREADABLE)
    CUSTOM_READABLE = (6100, "PresentMon-custom", "C:\\other-harness\\cache\\PresentMon-custom.exe")

    def test_an_owner_presentmonservice_does_not_skip_the_sweep(self) -> None:
        # Bachelor, 2026-10-03: the always-running PresentMonService matched `PresentMon*` and skipped the sweep on both legs
        code, sweep, d, out = self.run_start("service-alive", others=(self.SERVICE,), services=self.SERVICE_ROW)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["PresentMon", "MLVAttr3-old-killed-job"], "the orphans are swept with the service running")
        self.assertTrue(sweep["ran"], sweep)
        self.assertIsNone(sweep["skippedReason"])
        self.assertEqual(sweep["livePresentMonProcessIds"], [], "the service is not a live capture")
        self.assertEqual(sweep["excludedPresentMonProcesses"], [{"name": "PresentMonService", "pid": 7001, "service": "PresentMonService", "pathReadable": True, "nonCaptureIdentity": "registered_service"}], "recorded as evidence: name, pid, service, pathReadable (never the path)")
        self.assertEqual(sweep["remaining"], [])
        self.assertIn("excluded=PresentMonService:7001", (d / "trace.log").read_text(encoding="utf-8"))

    def test_a_registered_service_with_an_unreadable_path_does_not_skip_the_sweep(self) -> None:
        # the Bachelor shape: the non-elevated agent cannot read the SYSTEM service's path, but the pid is a registered service's pid
        code, sweep, d, out = self.run_start("service-unreadable", others=(self.SERVICE_UNREADABLE,), services=self.SERVICE_ROW)
        self.assertEqual(code, 0, out)
        self.assertTrue(sweep["ran"], sweep)
        self.assertEqual(self.terminated_names(self.calls(d)), ["PresentMon", "MLVAttr3-old-killed-job"])
        self.assertEqual(sweep["excludedPresentMonProcesses"], [{"name": "PresentMonService", "pid": 7001, "service": "PresentMonService", "pathReadable": False, "nonCaptureIdentity": "registered_service"}])

    def test_a_service_registered_under_another_pid_does_not_excuse_a_process(self) -> None:
        # still live, and (r2) with no positive identity it is a blocker for the harness sessions too: nothing is swept
        code, sweep, d, out = self.run_start("service-other-pid", others=(self.SERVICE_UNREADABLE,), services=((7002, "PresentMonService"),))
        self.assertEqual(code, 0, out)
        self.assertEqual(sweep["livePresentMonProcessIds"], [7001])
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_a_live_pinned_capture_beside_the_service_still_skips_the_sweep(self) -> None:
        code, sweep, d, out = self.run_start("service-and-capture", live_pids=(4242,), others=(self.SERVICE,), services=self.SERVICE_ROW)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual(sweep["livePresentMonProcessIds"], [4242], "only the pinned capture counts as live")
        self.assertEqual(sweep["excludedPresentMonProcesses"], [{"name": "PresentMonService", "pid": 7001, "service": "PresentMonService", "pathReadable": True, "nonCaptureIdentity": "registered_service"}])

    def test_a_pinned_capture_that_appears_mid_sweep_still_stops_it_with_the_service_running(self) -> None:
        code, sweep, d, out = self.run_start("service-mid-sweep", live_after_terminate_of="PresentMon", others=(self.SERVICE,), services=self.SERVICE_ROW)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["PresentMon"])
        self.assertEqual(sweep["abortedBefore"], "MLVAttr3-old-killed-job:presentmon_terminate")
        self.assertEqual(sweep["livePresentMonProcessIds"], [5151])

    # --- sol's repro: the Path getter throws access denied (or is null), the process is not a registered service -> live, a recorded reason, and the DEFAULT session is never swept under it.
    # BACHELOR-CUDA-PRESENTMON-CSV-MISSING-1 (Bachelor, 2026-10-27): this is the real Bachelor shape (Intel's PresentMonService, a child of IntelGraphicsSoftware.Service.exe, is no registered
    # service and its path is unreadable to the agent), so it skipped the WHOLE sweep on every leg; an agent tree-kill at 20:06:20Z then left MLVAttr3-...-150416 running and every later capture
    # wrote no CSV. r2 (Dot advisory on #707): it is let out of the harness_sessions blockers only on POSITIVE identity -- image PresentMonService AND parent IntelGraphicsSoftware.Service.exe
    # (Win32_Process) -- so its MLVAttr3-* orphans are swept (scope harness_sessions) and the default session is left and recorded as deferred.
    def test_h1_the_intel_presentmonservice_sweeps_the_harness_orphan_and_defers_the_default_session(self) -> None:
        code, sweep, d, out = self.run_start("h1-intel-child", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-old-killed-job"], "the r1b repair: the 16-of-16 missing-CSV orphan is swept")
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "", "and logman stop is not used either")
        self.assertTrue(sweep["ran"], sweep)
        self.assertIsNone(sweep["error"])
        self.assertIsNone(sweep["skippedReason"])
        self.assertEqual(sweep["scope"], "harness_sessions")
        self.assertEqual(sweep["listed"], ["MLVAttr3-old-killed-job"])
        self.assertEqual(sweep["deferred"], ["PresentMon"])
        self.assertEqual(sweep["remaining"], [])
        self.assertEqual(sweep["livePresentMonProcessIds"], [7001])
        self.assertEqual(sweep["excludedPresentMonProcesses"], [])
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1, "the capture itself still starts")
        trace = (d / "trace.log").read_text(encoding="utf-8")
        self.assertIn("scope=harness_sessions", trace)
        self.assertIn("deferred=PresentMon", trace)

    def test_h1_the_identity_evidence_is_recorded_in_the_snapshot_detail(self) -> None:
        _code, sweep, d, _out = self.run_start("h1-evidence", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE)
        self.assertEqual(sweep["livePresentMonProcesses"], [self.INTEL_DETAIL])
        self.assertIn("identified=PresentMonService:7001:intel_graphics_presentmonservice:IntelGraphicsSoftware.Service.exe", (d / "trace.log").read_text(encoding="utf-8"))

    def test_h1_a_null_path_intel_child_is_identified_too(self) -> None:
        code, sweep, d, out = self.run_start("h1-null-path", others=((7001, "PresentMonService", None),), processes=self.INTEL_TREE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-old-killed-job"])
        self.assertEqual(sweep["deferred"], ["PresentMon"])
        self.assertFalse(sweep["livePresentMonProcesses"][0]["pathReadable"])

    def test_h2_a_registered_service_presentmon_is_unchanged(self) -> None:
        # the registered-service arm (UM-SWEEP-OWNER-PRESENTMONSERVICE-1) is not live at all: scope all, the default session and the orphan both go, and no parent is read for it
        code, sweep, d, out = self.run_start("h2-registered", others=(self.SERVICE_UNREADABLE,), services=self.SERVICE_ROW, process_lookup_fails=True)
        self.assertEqual(code, 0, out)
        self.assertEqual((sweep["scope"], sweep["skippedReason"], sweep["error"]), ("all", None, None))
        self.assertEqual(self.terminated_names(self.calls(d)), ["PresentMon", "MLVAttr3-old-killed-job"])
        self.assertEqual([(r["name"], r["pid"], r["service"], r["pathReadable"]) for r in sweep["excludedPresentMonProcesses"]], [("PresentMonService", 7001, "PresentMonService", False)])

    def test_h2_the_registered_service_record_names_its_identity(self) -> None:
        _code, sweep, _d, _out = self.run_start("h2-identity", others=(self.SERVICE_UNREADABLE,), services=self.SERVICE_ROW)
        self.assertEqual(sweep["excludedPresentMonProcesses"], [{"name": "PresentMonService", "pid": 7001, "service": "PresentMonService", "pathReadable": False, "nonCaptureIdentity": "registered_service"}])

    def h3(self, name: str, **kw) -> tuple[dict, Path]:
        code, sweep, d, out = self.run_start(name, others=(self.SERVICE_UNREADABLE,), **kw)
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"], sweep)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive", "no positive identity: a blocker, nothing is swept")
        self.assertIsNone(sweep["error"], "a refused identity is no sweep error: the process is simply a blocker")
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1, "the capture itself still starts")
        self.assertIsNone(sweep["livePresentMonProcesses"][0].get("nonCaptureIdentity"))
        return sweep["livePresentMonProcesses"][0], d

    def test_h3_a_presentmonservice_under_another_parent_is_a_blocker(self) -> None:
        detail, _d = self.h3("h3-other-parent", processes=((7001, "PresentMonService", 901, 10), (901, "explorer.exe", 4, 5)))
        # adversary A5: the refusal says why
        self.assertEqual((detail.get("parentName"), detail.get("parentPid"), detail.get("identityError")), ("explorer.exe", 901, "parent_not_intel_graphics_service"))

    def test_a1_a_parent_started_after_the_child_across_an_offset_change_is_no_evidence(self) -> None:
        # adversary A1's input: the child started 06:55Z (01:55-05:00, daylight), its parent exited, and pid 900 now belongs to an Intel service started 07:10Z (01:10-06:00, standard). On the
        # wall clock the replacement parent reads EARLIER than the child; as instants it is later, so it is no evidence
        detail, _d = self.h3("a1-offset-change", processes=((7001, "PresentMonService", 900, "2026-11-01T01:55:00-05:00"), (900, "IntelGraphicsSoftware.Service.exe", 4, "2026-11-01T01:10:00-06:00")))
        self.assertEqual(detail.get("identityError"), "parent_started_after_child")

    def test_a1_the_cim_shape_a_local_datetime_is_compared_as_an_instant(self) -> None:
        # the real Win32_Process CreationDate is a LOCAL DateTime (the same instants converted to the host's zone). On a host whose zone leaves daylight time at 2026-11-01T07:00Z (US Central,
        # the venues' zone) the parent's wall clock reads 01:10 and the child's 01:55, so a wall-clock compare accepts the reused pid; on a UTC host the wall clocks already agree with the instants
        detail, _d = self.h3("a1-local", processes=((7001, "PresentMonService", 900, "local:2026-11-01T06:55:00Z"), (900, "IntelGraphicsSoftware.Service.exe", 4, "local:2026-11-01T07:10:00Z")))
        self.assertEqual(detail.get("identityError"), "parent_started_after_child")

    def test_a1_the_same_offsets_in_the_right_order_still_identify_the_intel_child(self) -> None:
        code, sweep, d, out = self.run_start("a1-offset-ok", others=(self.SERVICE_UNREADABLE,), processes=((7001, "PresentMonService", 900, "2026-11-01T01:10:00-06:00"), (900, "IntelGraphicsSoftware.Service.exe", 4, "2026-11-01T01:55:00-05:00")))
        self.assertEqual(code, 0, out)
        self.assertEqual(sweep["livePresentMonProcesses"][0]["nonCaptureIdentity"], "intel_graphics_presentmonservice", sweep)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-old-killed-job"])
    def test_h3_a_presentmonservice_whose_parent_read_throws_is_a_blocker_with_the_reason(self) -> None:
        detail, _d = self.h3("h3-read-throws", processes=self.INTEL_TREE, process_lookup_fails=True)
        self.assertIn("parent_read_failed", str(detail.get("identityError")))
        self.assertIn("stub Win32_Process failure", str(detail.get("identityError")))

    def test_h3_a_presentmonservice_with_no_process_row_is_a_blocker(self) -> None:
        detail, _d = self.h3("h3-no-row")
        self.assertEqual(detail.get("identityError"), "process_row_count=0")

    def test_h3_a_presentmonservice_whose_parent_is_gone_is_a_blocker(self) -> None:
        detail, _d = self.h3("h3-parent-gone", processes=((7001, "PresentMonService", 900, 10),))
        self.assertEqual((detail.get("parentPid"), detail.get("identityError")), (900, "parent_row_count=0"))

    def test_h3_a_parent_pid_reused_after_the_child_started_is_no_evidence(self) -> None:
        detail, _d = self.h3("h3-pid-reuse", processes=((7001, "PresentMonService", 900, 10), (900, "IntelGraphicsSoftware.Service.exe", 4, 20)))
        self.assertEqual((detail.get("parentName"), detail.get("identityError")), ("IntelGraphicsSoftware.Service.exe", "parent_started_after_child"))

    def test_h3_an_unreadable_creation_date_is_no_evidence(self) -> None:
        detail, _d = self.h3("h3-no-date", processes=((7001, "PresentMonService", 900, None), (900, "IntelGraphicsSoftware.Service.exe", 4, 5)))
        self.assertEqual(detail.get("identityError"), "creation_date_unreadable")

    def test_h3_only_the_exact_presentmonservice_name_can_carry_the_intel_identity(self) -> None:
        # the same Intel parent is no identity for any other image name
        code, sweep, d, out = self.run_start("h3-other-name", others=((7001, "PresentMonServiceX", UNREADABLE),), processes=((7001, "PresentMonServiceX", 900, 10), (900, "IntelGraphicsSoftware.Service.exe", 4, 5)))
        self.assertEqual(code, 0, out)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        detail = sweep["livePresentMonProcesses"][0]
        self.assertEqual((detail.get("nonCaptureIdentity"), detail.get("parentName")), (None, None), "no parent is read for another name")
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    # --- r2 regressions (Dot advisory on #707, hub-confirmed on cd2a518d): a second harness's live PresentMon-custom must stop the harness_sessions sweep at every one of its three gates
    def test_r1_a_live_presentmon_custom_with_an_unreadable_path_refuses_the_sweep(self) -> None:
        code, sweep, d, out = self.run_start("r1-custom-unreadable", others=(self.CUSTOM_UNREADABLE,))
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"], sweep)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive", "as on base 038ba335")
        self.assertEqual(sweep["livePresentMonProcessIds"], [6100])
        self.assertEqual(self.terminated_names(self.calls(d)), [], "the other harness's live MLVAttr3-* capture is never terminated")
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")

    def test_r1_a_live_presentmon_custom_with_a_readable_path_refuses_the_sweep(self) -> None:
        # the realistic form: the other harness runs under the same agent account, so its path IS readable; a readable path alone was an exclusion before r2 (scope all swept its session)
        code, sweep, d, out = self.run_start("r1-custom-readable", others=(self.CUSTOM_READABLE,))
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"], sweep)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual(sweep["excludedPresentMonProcesses"], [])
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_r1_a_presentmon_custom_beside_the_intel_child_still_refuses_the_sweep(self) -> None:
        code, sweep, d, out = self.run_start("r1-custom-beside-intel", others=(self.SERVICE_UNREADABLE, self.CUSTOM_UNREADABLE), processes=self.INTEL_TREE)
        self.assertEqual(code, 0, out)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_r2_a_presentmon_custom_that_appears_before_a_terminate_aborts_the_sweep(self) -> None:
        code, sweep, d, out = self.run_start("r2-custom-mid-terminate", sessions=("MLVAttr3-killed-a", "MLVAttr3-other-harness-live"), live_after_terminate_of="MLVAttr3-killed-a",
                                             go_live_as=self.CUSTOM_UNREADABLE, others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-killed-a"], "the other harness's session is never terminated")
        self.assertEqual(sweep["abortedReason"], "presentmon_process_started_during_sweep")
        self.assertEqual(sweep["abortedBefore"], "MLVAttr3-other-harness-live:presentmon_terminate")
        self.assertEqual(sweep["livePresentMonProcessIds"], [6100])
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")

    def test_r3_a_presentmon_custom_that_appears_before_a_logman_stop_aborts_the_sweep(self) -> None:
        code, sweep, d, out = self.run_start("r3-custom-before-stop", sessions=("MLVAttr3-killed-a", "MLVAttr3-other-harness-live"), helper_works=False,
                                             live_after_terminate_of="MLVAttr3-other-harness-live", go_live_as=self.CUSTOM_UNREADABLE, others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE)
        self.assertEqual(code, 0, out)
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "", "logman stop is never issued under the other harness's live capture")
        self.assertEqual(sweep["abortedReason"], "presentmon_process_started_during_sweep")
        self.assertEqual(sweep["abortedBefore"], "MLVAttr3-killed-a:logman_stop")
        self.assertEqual(sweep["remaining"], ["MLVAttr3-killed-a", "MLVAttr3-other-harness-live"])

    def test_mutation_a_name_filter_on_the_blockers_lets_the_sweep_stop_a_live_custom_capture_again(self) -> None:
        # the cd2a518d filter, restored: only capture-image names block -> Dot's repro
        mutated = mutate(TEMPLATE, "return @(@($Snapshot.live) | Where-Object { $identified -notcontains [int]$_ } | ForEach-Object { [int]$_ })",
                         "return @(@($Snapshot.liveDetail) | Where-Object { ([string]$_.name) -imatch '^PresentMon([-_.]?(\\d|x64|x86|console|capture|cli)|$)' } | ForEach-Object { [int]$_.pid })")
        _code, sweep, d, _out = self.run_start("mut-name-filter", others=(self.CUSTOM_UNREADABLE,), text=mutated)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-old-killed-job"], "the mutation terminates the other harness's live session")

    def test_mutation_without_the_parent_name_check_any_parent_identifies_the_service(self) -> None:
        mutated = mutate(TEMPLATE, "if ($result.parentName -ieq 'IntelGraphicsSoftware.Service.exe') {", "if ($true) {")
        _code, sweep, _d, _out = self.run_start("mut-parent-name", others=(self.SERVICE_UNREADABLE,), processes=((7001, "PresentMonService", 901, 10), (901, "explorer.exe", 4, 5)), text=mutated)
        self.assertTrue(sweep["ran"], "the mutation lets a PresentMonService under any parent out of the blockers")

    def test_mutation_without_the_name_check_any_image_under_the_intel_parent_is_identified(self) -> None:
        mutated = mutate(TEMPLATE, "if ($Name -ine 'PresentMonService') { return $result }", "")
        _code, sweep, _d, _out = self.run_start("mut-child-name", others=((7001, "PresentMonServiceX", UNREADABLE),), processes=((7001, "PresentMonServiceX", 900, 10), (900, "IntelGraphicsSoftware.Service.exe", 4, 5)), text=mutated)
        self.assertTrue(sweep["ran"])

    def test_mutation_without_the_creation_order_check_a_reused_parent_pid_identifies_the_service(self) -> None:
        mutated = mutate(TEMPLATE, "if ($instants[1] -gt $instants[0]) {", "if ($false) {")
        _code, sweep, _d, _out = self.run_start("mut-creation", others=(self.SERVICE_UNREADABLE,), processes=((7001, "PresentMonService", 900, 10), (900, "IntelGraphicsSoftware.Service.exe", 4, 20)), text=mutated)
        self.assertTrue(sweep["ran"])

    def test_mutation_a_parent_read_failure_read_as_identity_is_not_fail_closed(self) -> None:
        mutated = mutate(TEMPLATE, "        $result['nonCaptureIdentity'] = $null\n        $result['identityError'] = 'parent_read_failed: ' + $_.Exception.Message",
                         "        $result['nonCaptureIdentity'] = 'intel_graphics_presentmonservice'\n        $result['identityError'] = 'parent_read_failed: ' + $_.Exception.Message")
        _code, sweep, _d, _out = self.run_start("mut-read-open", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE, process_lookup_fails=True, text=mutated)
        self.assertTrue(sweep["ran"], "the mutation sweeps on a parent it could not read")

    # --- r2 item 3 (Dot's improvement on #707): newlyListed is what the FULL initial listing lacked
    def test_item3_a_deferred_default_session_is_never_reported_as_newly_listed(self) -> None:
        _code, sweep, _d, _out = self.run_start("item3-deferred-not-new", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE)
        self.assertEqual(sweep["deferred"], ["PresentMon"])
        self.assertEqual(sweep["newlyListed"], [], "the default session was in the first listing: it is not new")

    def test_item3_a_session_that_really_appeared_is_still_newly_listed_in_harness_scope(self) -> None:
        _code, sweep, d, _out = self.run_start("item3-really-new", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE, extra_from_query=2)
        self.assertEqual(sweep["newlyListed"], ["MLVAttr3-another-live-job"])
        self.assertNotIn("MLVAttr3-another-live-job", self.terminated_names(self.calls(d)))

    def test_mutation_newly_listed_against_the_filtered_list_reports_the_deferred_session_again(self) -> None:
        mutated = mutate(TEMPLATE, "$record['newlyListed'] = @($finalNames | Where-Object { $initialNames -notcontains $_ })", "$record['newlyListed'] = @($finalNames | Where-Object { $listed -notcontains $_ })")
        _code, sweep, _d, _out = self.run_start("mut-newly", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE, text=mutated)
        self.assertEqual(sweep["newlyListed"], ["PresentMon"])

    # --- r3 regressions (sol B2 on 2519ce4f, Dot advisory, hub-confirmed): -PresentMonName accepts any <name>.exe, so a second harness's LIVE capture can run as capture.exe. Beside the
    # identified Intel service the r2 scope was harness_sessions and the PresentMon*-only enumeration never saw it: its live MLVAttr3-* session was terminated (base 038ba335 skipped instead).
    OTHER_SESSIONS = ("PresentMon", "MLVAttr3-old-killed-job", "MLVAttr3-other-harness-live")
    CAPTURE = (6100, "capture", "C:\\other-harness\\cache\\capture.exe")
    CAPTURE_CMD = ('"C:\\other-harness\\cache\\capture.exe" --process_name MLVApp.exe --output_file C:\\other-harness\\out\\presentmon.csv --timed 60 --terminate_after_timed '
                   '--session_name MLVAttr3-other-harness-live --stop_existing_session --no_console_stats')

    def assert_nothing_destructive(self, d: Path) -> None:
        self.assertEqual(self.terminated_names(self.calls(d)), [], "the other harness's live MLVAttr3-* capture is never terminated")
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "", "and logman stop is never issued")
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1, "the capture itself still starts")

    def test_r3_r1_a_renamed_capture_beside_the_intel_service_refuses_the_sweep(self) -> None:
        code, sweep, d, out = self.run_start("r3-r1-renamed-beside-intel", sessions=self.OTHER_SESSIONS, others=(self.SERVICE_UNREADABLE, self.CAPTURE), processes=self.INTEL_TREE,
                                             command_lines=((6100, self.CAPTURE_CMD),))
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"], sweep)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive", "as on base 038ba335: no harness_sessions scope")
        self.assertIsNone(sweep["scope"])
        self.assertIn(6100, sweep["livePresentMonProcessIds"])
        self.assertEqual({r["pid"]: r["reason"] for r in sweep["livePresentMonProcesses"]}[6100], "capture_session_command_line")
        self.assert_nothing_destructive(d)

    def test_r3_r1_a_renamed_capture_whose_command_line_is_unreadable_is_found_by_its_image_hash(self) -> None:
        # the same capture, its command line unreadable (no row): a copy of the pinned binary under another name and path is found by the pinned SHA-256
        image = self.stmp / "r3-r1-hash-other-harness" / "capture.exe"
        code, sweep, d, out = self.run_start("r3-r1-hash", sessions=self.OTHER_SESSIONS, others=(self.SERVICE_UNREADABLE, (6100, "capture", str(image))), processes=self.INTEL_TREE,
                                             presentmon_sha="PINNED", pinned_copies=(image,))
        self.assertEqual(code, 0, out)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual({r["pid"]: r["reason"] for r in sweep["livePresentMonProcesses"]}[6100], "pinned_image_sha256")
        self.assertEqual(sweep["unreadableCommandLineCount"], 2, "the Intel child and the capture had no readable command line; recorded, neither a blocker by that alone")
        self.assert_nothing_destructive(d)

    def test_r3_r1_a_capture_under_the_configured_name_is_live_whatever_its_prefix(self) -> None:
        # this harness configured with -PresentMonName capture.exe (here a .cmd stub): its own pinned image never matched PresentMon* and was invisible too
        code, sweep, d, out = self.run_start("r3-r1-configured", sessions=self.OTHER_SESSIONS, others=(self.SERVICE_UNREADABLE, (6101, "capture", UNREADABLE)), processes=self.INTEL_TREE,
                                             pm_name="capture.cmd")
        self.assertEqual(code, 0, out)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual({r["pid"]: r["reason"] for r in sweep["livePresentMonProcesses"]}[6101], "pinned_image_name")
        self.assert_nothing_destructive(d)

    def test_r3_r1_a_single_dash_session_name_argument_is_a_candidate_too(self) -> None:
        code, sweep, d, out = self.run_start("r3-r1-single-dash", sessions=self.OTHER_SESSIONS, others=(self.SERVICE_UNREADABLE, (6102, "pm", "C:\\x\\pm.exe")), processes=self.INTEL_TREE,
                                             command_lines=((6102, "pm.exe -session_name OtherCapture -process_name MLVApp.exe"),))
        self.assertEqual(code, 0, out)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assert_nothing_destructive(d)

    def test_r3_r1_the_sweeping_job_itself_is_never_a_candidate(self) -> None:
        # the job's own process ($PID) owns no ETW session; were it a candidate, a job whose command line names its session would never sweep
        code, sweep, d, out = self.run_start("r3-r1-self", others=(self.SERVICE_UNREADABLE, ("$PID", "pwsh", "C:\\pwsh\\pwsh.exe")), processes=self.INTEL_TREE,
                                             command_lines=(("$PID", "pwsh -File job.ps1 MLVAttr3-the-new-job"),))
        self.assertEqual(code, 0, out)
        self.assertTrue(sweep["ran"], sweep)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-old-killed-job"])

    def test_r3_r2_a_renamed_capture_that_appears_before_a_terminate_aborts_the_sweep(self) -> None:
        code, sweep, d, out = self.run_start("r3-r2-renamed-mid-terminate", sessions=("MLVAttr3-killed-a", "MLVAttr3-other-harness-live"), live_after_terminate_of="MLVAttr3-killed-a",
                                             go_live_as=self.CAPTURE, go_live_command_line=self.CAPTURE_CMD, others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-killed-a"], "the other harness's session is never terminated")
        self.assertEqual(sweep["abortedReason"], "presentmon_process_started_during_sweep")
        self.assertEqual(sweep["abortedBefore"], "MLVAttr3-other-harness-live:presentmon_terminate")
        self.assertEqual(sweep["livePresentMonProcessIds"], [6100])
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")

    def test_r3_r3_a_renamed_capture_that_appears_before_a_logman_stop_aborts_the_sweep(self) -> None:
        code, sweep, d, out = self.run_start("r3-r3-renamed-before-stop", sessions=("MLVAttr3-killed-a", "MLVAttr3-other-harness-live"), helper_works=False,
                                             live_after_terminate_of="MLVAttr3-other-harness-live", go_live_as=self.CAPTURE, go_live_command_line=self.CAPTURE_CMD,
                                             others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE)
        self.assertEqual(code, 0, out)
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "", "logman stop is never issued under the other harness's live capture")
        self.assertEqual(sweep["abortedReason"], "presentmon_process_started_during_sweep")
        self.assertEqual(sweep["abortedBefore"], "MLVAttr3-killed-a:logman_stop")
        self.assertEqual(sweep["remaining"], ["MLVAttr3-killed-a", "MLVAttr3-other-harness-live"])

    def test_r3_r4_a_command_line_query_that_fails_is_a_typed_fail_closed_skip(self) -> None:
        for label, others, processes in (("beside-intel", (self.SERVICE_UNREADABLE,), self.INTEL_TREE), ("no-presentmon", (), ())):
            code, sweep, d, out = self.run_start(f"r3-r4-{label}", others=others, processes=processes, command_line_query_fails_from=1)
            self.assertEqual(code, 0, out)
            self.assertFalse(sweep["ran"], (label, sweep))
            self.assertEqual(sweep["skippedReason"], "capture_owner_command_lines_unreadable", label)
            self.assertIsNone(sweep["scope"], "no scope is chosen, harness_sessions least of all")
            self.assertIn("stub Win32_Process command-line query failure", sweep["commandLineError"], label)
            self.assert_nothing_destructive(d)

    def test_r3_r4_a_command_line_query_that_fails_on_a_recheck_stops_the_sweep_with_a_typed_error(self) -> None:
        # queries: 1 the scope choice, 2 the post-listing recheck, 3 the recheck before the first terminate -- it fails there
        code, sweep, d, out = self.run_start("r3-r4-recheck", sessions=("MLVAttr3-killed-a", "MLVAttr3-killed-b"), others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE,
                                             command_line_query_fails_from=3)
        self.assertEqual(code, 0, out)
        self.assertIn("capture_owner_command_lines_unreadable", str(sweep["error"]))
        self.assert_nothing_destructive(d)

    def test_r3_the_intel_service_alone_still_sweeps_with_readable_command_lines(self) -> None:
        # the healthy control with the command-line route active: the Intel child's own command line names no session
        code, sweep, d, out = self.run_start("r3-intel-readable", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE,
                                             command_lines=((7001, "\"C:\\Program Files\\Intel\\PresentMonService.exe\""),))
        self.assertEqual(code, 0, out)
        self.assertEqual((sweep["scope"], sweep["deferred"], sweep["unreadableCommandLineCount"]), ("harness_sessions", ["PresentMon"], 0))
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-old-killed-job"])

    # --- r4 regressions (Dot advisory on a79a5a89, hub-confirmed): a process whose path is unreadable AND whose command line reads empty, under a name no route matches, was dropped with
    # no record. Beside the identified Intel service the scope was harness_sessions and the sweep could terminate that capture's live MLVAttr3-* session (base 038ba335 skipped).
    UNIDENTIFIED = (6200, "capture", UNREADABLE)

    def assert_refused_unidentified(self, sweep: dict, d: Path, label: str = "") -> None:
        self.assertFalse(sweep["ran"], (label, sweep))
        self.assertEqual(sweep["skippedReason"], "capture_owner_unidentified", (label, sweep))
        self.assertIsNone(sweep["scope"], "no harness_sessions scope")
        self.assertEqual(sweep["unidentifiedCaptureOwnerIds"], [6200], label)
        self.assert_nothing_destructive(d)

    def test_r4_u1_an_unidentified_capture_beside_the_intel_service_refuses_the_harness_scope(self) -> None:
        # the capture runs in the job's own session; with the job's session unknown or 0, a session-0 process has no evidence either
        for label, job_session, capture_session in (("same-session", 1, 1), ("job-session-unknown", None, 0), ("job-in-session-0", 0, 0), ("other-interactive-session", 1, 2)):
            code, sweep, d, out = self.run_start(f"r4-u1-{label}", sessions=self.OTHER_SESSIONS, others=(self.SERVICE_UNREADABLE, self.UNIDENTIFIED), processes=self.INTEL_TREE,
                                                 job_session=job_session, session_ids={6200: capture_session})
            self.assertEqual(code, 0, out)
            self.assert_refused_unidentified(sweep, d, label)
            self.assertEqual([(u["pid"], u["name"], u["sessionId"]) for u in sweep["unidentifiedCaptureOwners"]], [(6200, "capture", capture_session)], label)
            trace = (d / "trace.log").read_text(encoding="utf-8")
            self.assertIn("skipped=capture_owner_unidentified", trace, label)
            self.assertIn("unidentifiedBlocking=6200", trace, label)

    def test_r4_u2_an_unidentified_capture_that_appears_before_a_terminate_aborts_the_sweep(self) -> None:
        code, sweep, d, out = self.run_start("r4-u2-mid-terminate", sessions=("MLVAttr3-killed-a", "MLVAttr3-other-harness-live"), live_after_terminate_of="MLVAttr3-killed-a",
                                             go_live_as=self.UNIDENTIFIED, others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE, job_session=1, session_ids={6200: 1})
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-killed-a"], "the unidentified capture's session is never terminated")
        self.assertEqual(sweep["abortedReason"], "capture_owner_unidentified")
        self.assertEqual(sweep["abortedBefore"], "MLVAttr3-other-harness-live:presentmon_terminate")
        self.assertEqual(sweep["unidentifiedCaptureOwnerIds"], [6200])
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "", "no logman stop after an abort")
        self.assertEqual(sweep["remaining"], ["MLVAttr3-other-harness-live"])

    def test_r4_u3_an_unidentified_capture_that_appears_before_a_logman_stop_aborts_the_sweep(self) -> None:
        code, sweep, d, out = self.run_start("r4-u3-before-stop", sessions=("MLVAttr3-killed-a", "MLVAttr3-other-harness-live"), helper_works=False,
                                             live_after_terminate_of="MLVAttr3-other-harness-live", go_live_as=self.UNIDENTIFIED, others=(self.SERVICE_UNREADABLE,),
                                             processes=self.INTEL_TREE, job_session=1, session_ids={6200: 1})
        self.assertEqual(code, 0, out)
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "", "logman stop is never issued under the unidentified capture")
        self.assertEqual(sweep["abortedReason"], "capture_owner_unidentified")
        self.assertEqual(sweep["abortedBefore"], "MLVAttr3-killed-a:logman_stop")
        self.assertEqual(sweep["remaining"], ["MLVAttr3-killed-a", "MLVAttr3-other-harness-live"])

    def test_r4_u4_a_session_probe_that_fails_is_no_evidence(self) -> None:
        # the capture's own session unreadable (getter throws, or a null value), or the JOB's session unreadable while the capture reads session 0: fail closed, never a made-up session
        for label, job_session, capture_session in (("capture-getter-throws", 1, SESSION_THROWS), ("capture-session-null", 1, None), ("job-getter-throws", SESSION_THROWS, 0)):
            code, sweep, d, out = self.run_start(f"r4-u4-{label}", sessions=self.OTHER_SESSIONS, others=(self.SERVICE_UNREADABLE, self.UNIDENTIFIED), processes=self.INTEL_TREE,
                                                 job_session=job_session, session_ids={6200: capture_session})
            self.assertEqual(code, 0, out)
            self.assert_refused_unidentified(sweep, d, label)
            owner = sweep["unidentifiedCaptureOwners"][0]
            if label == "job-getter-throws":
                self.assertEqual(sweep["unidentifiedNonOwnership"]["jobSessionError"], "session_id_unreadable", label)
            else:
                self.assertIsNone(owner["sessionId"], label)
                self.assertEqual(owner["sessionError"], "session_id_unreadable", label)

    # controls: GREEN on a79a5a89 and after (they read only fields both heads record)
    def test_r4_control_an_unreadable_process_in_session_0_beside_the_intel_service_does_not_block(self) -> None:
        # the positive non-ownership evidence: session 0 while the job runs in session 1 (the measured host shape: every service and system process)
        code, sweep, d, out = self.run_start("r4-c-session-0", others=(self.SERVICE_UNREADABLE, (6201, "svchost", UNREADABLE)), processes=self.INTEL_TREE,
                                             job_session=1, session_ids={6201: 0})
        self.assertEqual(code, 0, out)
        self.assertTrue(sweep["ran"], sweep)
        self.assertEqual(sweep["scope"], "harness_sessions")
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-old-killed-job"], "the r1b repair: the harness orphan is swept")

    def test_r4_control_an_unreadable_registered_service_beside_the_intel_service_does_not_block(self) -> None:
        # the job's session unknown, so only the registration covers it
        code, sweep, d, out = self.run_start("r4-c-service", others=(self.SERVICE_UNREADABLE, (6202, "SomeVendorService", UNREADABLE)), processes=self.INTEL_TREE,
                                             services=((6202, "SomeVendorSvc"),))
        self.assertEqual(code, 0, out)
        self.assertTrue(sweep["ran"], sweep)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-old-killed-job"])

    def test_r4_control_scope_all_is_unchanged_by_an_unidentified_process(self) -> None:
        # no live candidate at all: scope 'all' keeps its behaviour (card r4 item 1)
        code, sweep, d, out = self.run_start("r4-c-scope-all", others=(self.UNIDENTIFIED,), job_session=1, session_ids={6200: 1})
        self.assertEqual(code, 0, out)
        self.assertTrue(sweep["ran"], sweep)
        self.assertEqual(sweep["scope"], "all")
        self.assertEqual(self.terminated_names(self.calls(d)), ["PresentMon", "MLVAttr3-old-killed-job"])

    def test_r4_the_evidence_is_recorded(self) -> None:
        code, sweep, d, out = self.run_start("r4-evidence", others=(self.SERVICE_UNREADABLE, (6201, "svchost", UNREADABLE), (6202, "SomeVendorService", UNREADABLE)),
                                             processes=self.INTEL_TREE, services=((6202, "SomeVendorSvc"),), job_session=1, session_ids={6201: 0, 6202: 1})
        self.assertEqual(code, 0, out)
        self.assertTrue(sweep["ran"], sweep)
        self.assertEqual(sweep["unidentifiedCaptureOwners"], [])
        self.assertEqual(sweep["unidentifiedNonOwnership"], {"jobSessionId": 1, "jobSessionError": None, "sessionZeroCount": 1, "registeredServiceCount": 1})

    def test_mutation_without_the_unreadable_identity_hold_the_unidentified_capture_is_swept_again(self) -> None:
        mutated = mutate(TEMPLATE, "{ $unreadableIdentity += [pscustomobject]@{", "{ $null = [pscustomobject]@{")
        _code, sweep, d, _out = self.run_start("mut-r4-no-hold", sessions=self.OTHER_SESSIONS, others=(self.SERVICE_UNREADABLE, self.UNIDENTIFIED), processes=self.INTEL_TREE,
                                               job_session=1, session_ids={6200: 1}, text=mutated)
        self.assertIn("MLVAttr3-other-harness-live", self.terminated_names(self.calls(d)), "Dot's residual: the live session is terminated")

    def test_mutation_without_the_held_service_lookup_a_registered_service_blocks_the_harness_scope(self) -> None:
        mutated = mutate(TEMPLATE, "if ($heldService.Count -gt 0) {", "if ($false) {")
        _code, sweep, d, _out = self.run_start("mut-r4-held-service", others=(self.SERVICE_UNREADABLE, (6202, "SomeVendorService", UNREADABLE)), processes=self.INTEL_TREE,
                                               services=((6202, "SomeVendorSvc"),), text=mutated)
        self.assertEqual(sweep["skippedReason"], "capture_owner_unidentified", "the registration is what cleared it")

    def test_mutation_session_0_without_the_job_session_condition_is_no_evidence(self) -> None:
        mutated = mutate(TEMPLATE, "$held.session.sessionId -eq 0 -and $jobSession.sessionId -ne 0", "$held.session.sessionId -eq 0")
        _code, sweep, d, _out = self.run_start("mut-r4-session", sessions=self.OTHER_SESSIONS, others=(self.SERVICE_UNREADABLE, self.UNIDENTIFIED), processes=self.INTEL_TREE,
                                               job_session=0, session_ids={6200: 0}, text=mutated)
        self.assertIn("MLVAttr3-other-harness-live", self.terminated_names(self.calls(d)), "a harness job in session 0 owns captures in session 0")

    def test_mutation_rechecks_that_ignore_the_unidentified_terminate_under_it(self) -> None:
        mutated = TEMPLATE
        for old in ("            if (@($recheck.unidentified).Count -gt 0) {\n                $record['unidentifiedCaptureOwnerIds'] = @($recheck.unidentified)\n                $record['abortedReason']",
                    "                if (@($recheck.unidentified).Count -gt 0) {\n                    $record['unidentifiedCaptureOwnerIds'] = @($recheck.unidentified)\n                    $record['abortedReason']"):
            mutated = mutate(mutated, old, old.replace("if (@($recheck.unidentified).Count -gt 0) {", "if ($false) {"))
        _code, sweep, d, _out = self.run_start("mut-r4-recheck", sessions=("MLVAttr3-killed-a", "MLVAttr3-other-harness-live"), live_after_terminate_of="MLVAttr3-killed-a",
                                               go_live_as=self.UNIDENTIFIED, others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE, job_session=1, session_ids={6200: 1}, text=mutated)
        self.assertIn("MLVAttr3-other-harness-live", self.terminated_names(self.calls(d)))

    def test_mutation_the_presentmon_star_enumeration_terminates_a_renamed_live_capture_again(self) -> None:
        mutated = mutate(TEMPLATE, "foreach ($candidate in @(Get-Process -ErrorAction Stop)) {", "foreach ($candidate in @(Get-Process -Name 'PresentMon*' -ErrorAction Stop)) {")
        _code, sweep, d, _out = self.run_start("mut-r3-name-only", sessions=self.OTHER_SESSIONS, others=(self.SERVICE_UNREADABLE, self.CAPTURE), processes=self.INTEL_TREE,
                                               command_lines=((6100, self.CAPTURE_CMD),), text=mutated)
        self.assertIn("MLVAttr3-other-harness-live", self.terminated_names(self.calls(d)), "sol B2: the live session is terminated")

    def test_mutation_without_the_command_line_route_a_renamed_capture_is_invisible_again(self) -> None:
        mutated = mutate(TEMPLATE, "$namesSession = ($commandLine -match $sessionCommandLinePattern)", "$namesSession = $false")
        _code, sweep, d, _out = self.run_start("mut-r3-no-cmdline", sessions=self.OTHER_SESSIONS, others=(self.SERVICE_UNREADABLE, self.CAPTURE), processes=self.INTEL_TREE,
                                               command_lines=((6100, self.CAPTURE_CMD),), text=mutated)
        self.assertIn("MLVAttr3-other-harness-live", self.terminated_names(self.calls(d)))

    def test_mutation_a_failed_command_line_query_read_as_none_is_not_fail_closed(self) -> None:
        mutated = mutate(TEMPLATE, "        if ($null -ne $snapshot.commandLineError) {\n            $record['skippedReason'] = 'capture_owner_command_lines_unreadable'",
                         "        if ($false) {\n            $record['skippedReason'] = 'capture_owner_command_lines_unreadable'")
        _code, sweep, d, _out = self.run_start("mut-r3-cim-open", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE, command_line_query_fails_from=1, text=mutated)
        # r4: the scope choice now asks the shared helper, which throws on the same error, so the mutation loses the TYPED skip (an untyped error is left) but still touches nothing
        self.assertIsNone(sweep["skippedReason"], "the mutation drops the typed fail-closed skip")
        self.assertIn("capture_owner_command_lines_unreadable", str(sweep["error"]))
        self.assert_nothing_destructive(d)

    def test_mutation_a_recheck_that_ignores_the_failed_query_terminates_on_it(self) -> None:
        mutated = mutate(TEMPLATE, "if ($null -ne $snapshot.commandLineError) { throw ('capture_owner_command_lines_unreadable: ' + $snapshot.commandLineError) }", "")
        _code, sweep, d, _out = self.run_start("mut-r3-recheck-open", sessions=("MLVAttr3-killed-a", "MLVAttr3-killed-b"), others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE,
                                               command_line_query_fails_from=3, text=mutated)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-killed-a", "MLVAttr3-killed-b"], "the mutation terminates under a blind recheck")

    def test_the_bachelor_service_child_never_aborts_the_harness_sweep_on_its_rechecks(self) -> None:
        # the service is alive through EVERY liveness re-check; in harness scope it is positively identified, so both leftover job sessions go
        code, sweep, d, out = self.run_start("service-child-rechecks", sessions=("MLVAttr3-killed-a", "MLVAttr3-killed-b"), others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-killed-a", "MLVAttr3-killed-b"])
        self.assertIsNone(sweep["abortedReason"], sweep)
        self.assertEqual((sweep["deferred"], sweep["remaining"]), ([], []))

    def test_in_harness_scope_a_capture_image_that_appears_mid_sweep_still_stops_it(self) -> None:
        code, sweep, d, out = self.run_start("service-child-mid-sweep", sessions=("MLVAttr3-killed-a", "MLVAttr3-killed-b"), live_after_terminate_of="MLVAttr3-killed-a",
                                             others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["MLVAttr3-killed-a"])
        self.assertEqual(sweep["abortedBefore"], "MLVAttr3-killed-b:presentmon_terminate")
        self.assertEqual(sweep["remaining"], ["MLVAttr3-killed-b"])

    def test_a_capture_image_beside_the_service_child_still_skips_the_whole_sweep(self) -> None:
        code, sweep, d, out = self.run_start("service-child-and-capture", others=(self.SERVICE_UNREADABLE, (4900, "PresentMon-2.4.1-x64", UNREADABLE)), processes=self.INTEL_TREE)
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"], sweep)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_mutation_the_service_child_skips_the_whole_sweep_again(self) -> None:
        mutated = mutate(TEMPLATE, "if (@($initial.live).Count -gt 0) {", "if ($true) {")
        _code, sweep, d, _out = self.run_start("mut-service-child", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE, text=mutated)
        self.assertFalse(sweep["ran"], "the 2026-10-27 Bachelor state: the orphan of a killed leg is never swept")
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_mutation_rechecks_that_count_every_live_process_abort_the_harness_sweep(self) -> None:
        mutated = mutate(TEMPLATE, "if ($Scope -eq 'harness_sessions') { return [pscustomobject]@{ live = @(Get-PresentMonCaptureBlockerId $snapshot);", "if ($false) { return [pscustomobject]@{ live = @(Get-PresentMonCaptureBlockerId $snapshot);")
        _code, sweep, d, _out = self.run_start("mut-recheck-scope", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE, text=mutated)
        # r4: the scope choice asks the same helper now, so the mutation refuses there already (was: at the first re-check)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_mutation_without_the_scope_filter_the_default_session_is_swept_under_an_unidentified_process(self) -> None:
        mutated = mutate(TEMPLATE, "$listed = @($listed | Where-Object { $_ -like 'MLVAttr3-*' })", "")
        _code, sweep, d, _out = self.run_start("mut-scope-filter", others=(self.SERVICE_UNREADABLE,), processes=self.INTEL_TREE, text=mutated)
        self.assertIn("PresentMon", self.terminated_names(self.calls(d)))

    def test_an_unreadable_foreign_capture_of_another_version_is_live(self) -> None:
        code, sweep, d, out = self.run_start("foreign-unreadable", others=((4900, "PresentMon-2.4.1-x64", UNREADABLE),))
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"], sweep)
        self.assertEqual(sweep["livePresentMonProcessIds"], [4900])
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_a_readable_foreign_capture_image_that_is_no_service_is_live(self) -> None:
        # another PresentMon version, or a plain PresentMon.exe, in a console capture: its path is readable but its image is a capture image
        code, sweep, d, out = self.run_start("foreign-readable-capture", others=((4901, "PresentMon-2.4.1-x64", "C:\\tools\\PresentMon-2.4.1-x64.exe"), (4902, "PresentMon", "C:\\tools\\PresentMon.exe")))
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"], sweep)
        self.assertEqual(sweep["livePresentMonProcessIds"], [4901, 4902])
        self.assertEqual([r["reason"] for r in sweep["livePresentMonProcesses"]], ["capture_image_name", "capture_image_name"])
        self.assertTrue(all(r["pathReadable"] for r in sweep["livePresentMonProcesses"]))
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_a_readable_path_that_is_neither_a_service_nor_a_capture_image_is_live(self) -> None:
        # r2: a readable path is no identity (a second harness under the same account has one), so this is live and, with no positive identity, a blocker for every scope
        code, sweep, d, out = self.run_start("readable-helper", others=((4950, "PresentMonOverlayHelper", "C:\\Program Files\\Vendor\\overlay.exe"),))
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"], sweep)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual(sweep["excludedPresentMonProcesses"], [])
        self.assertEqual(sweep["livePresentMonProcesses"][0]["reason"], "readable_path_not_a_registered_service")

    def test_a_service_lookup_that_fails_touches_nothing(self) -> None:
        code, sweep, d, out = self.run_start("lookup-fails", others=(self.SERVICE,), services=self.SERVICE_ROW, service_lookup_fails=True)
        self.assertEqual(code, 0, out)
        self.assertIn("stub Get-CimInstance failure", sweep["error"])
        self.assertFalse(sweep["ran"])
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1, "the capture itself still starts")

    def test_the_service_lookup_is_never_asked_when_no_process_needs_identifying(self) -> None:
        code, sweep, d, out = self.run_start("no-lookup", service_lookup_fails=True)
        self.assertEqual(code, 0, out)
        self.assertTrue(sweep["ran"], sweep)
        self.assertIsNone(sweep["error"])

    def test_the_pinned_executable_is_matched_by_path_as_well_as_by_name(self) -> None:
        # a copy of the pinned binary that carries another PresentMon* name is still a live capture: its image path is the pinned cache path (the enumeration only returns PresentMon* names, so the
        # name must start with PresentMon -- a name that does not is never returned by Get-Process -Name 'PresentMon*')
        code, sweep, d, out = self.run_start("by-path", others=((4343, "PresentMon-renamed", str(self.stmp / "by-path" / "cache" / STUB_EXE)),))
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["livePresentMonProcessIds"], [4343])
        self.assertEqual(sweep["livePresentMonProcesses"][0]["reason"], "pinned_image_path")
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_a_renamed_copy_at_the_pinned_path_is_live_whatever_its_name(self) -> None:
        # r3 (sol B2): the enumeration used to be Get-Process -Name 'PresentMon*' (the stub honours -Name like the real cmdlet), so this process was never examined and the sweep ran
        code, sweep, d, out = self.run_start("renamed-at-pinned-path", others=((4344, "renamed-capture", str(self.stmp / "renamed-at-pinned-path" / "cache" / STUB_EXE)),))
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"], sweep)
        self.assertEqual(sweep["livePresentMonProcessIds"], [4344])
        self.assertEqual(sweep["livePresentMonProcesses"][0]["reason"], "pinned_image_path")
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_a_process_that_is_no_candidate_by_any_route_is_never_a_blocker(self) -> None:
        # the honest scope of the wider enumeration: another name, another path, another hash, and a command line naming no session -- not examined, the sweep runs
        code, sweep, d, out = self.run_start("not-a-candidate", others=((4345, "explorer", "C:\\Windows\\explorer.exe"),), command_lines=((4345, "C:\\Windows\\explorer.exe /factory"),))
        self.assertEqual(code, 0, out)
        self.assertTrue(sweep["ran"], sweep)
        self.assertEqual((sweep["livePresentMonProcessIds"], sweep["excludedPresentMonProcesses"], sweep["scope"]), ([], [], "all"))

    def test_the_pinned_name_is_live_whatever_its_path_looks_like(self) -> None:
        # the path may read as a long name while $Cache is spelled short (8.3: RUNNER~1), or not read at all: the image name decides and the capture is live either way
        stub = Path(STUB_EXE).stem
        for label, path in (("long", "C:\\Users\\runneradmin\\AppData\\Local\\Temp\\cache\\" + STUB_EXE), ("unreadable", UNREADABLE), ("null", None)):
            code, sweep, d, out = self.run_start(f"pinned-{label}", others=((4360, stub, path),))
            self.assertEqual(code, 0, out)
            self.assertFalse(sweep["ran"], (label, sweep))
            self.assertEqual(sweep["livePresentMonProcessIds"], [4360], label)
            self.assertEqual(sweep["livePresentMonProcesses"][0]["reason"], "pinned_image_name", label)
            self.assertEqual(self.terminated_names(self.calls(d)), [], label)

    def test_the_image_name_is_matched_exactly_not_by_prefix_and_without_regard_to_case(self) -> None:
        stub = Path(STUB_EXE).stem
        code, sweep, d, out = self.run_start("exact-name", others=((4401, f"{stub}-helper", "C:\\Vendor\\helper.exe"), (4402, stub.upper(), "C:\\Vendor\\copy.exe")))
        self.assertEqual(code, 0, out)
        # r2: both are live now (a readable path is no identity); the exact name decides only WHICH reason
        self.assertEqual({r["pid"]: r["reason"] for r in sweep["livePresentMonProcesses"]}, {4401: "readable_path_not_a_registered_service", 4402: "pinned_image_name"},
                         "an upper-case image name of the pinned executable is the pinned image; a longer name that merely starts with it is not")

    def test_an_unreadable_process_list_still_touches_nothing(self) -> None:
        code, sweep, d, out = self.run_start("service-list-throws", get_process_throws=True, others=(self.SERVICE,), services=self.SERVICE_ROW)
        self.assertEqual(code, 0, out)
        self.assertIn("stub Get-Process failure", sweep["error"])
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertFalse(sweep["ran"])

    def test_the_process_list_is_read_with_an_error_action_that_throws(self) -> None:
        snapshot = _slice(TEMPLATE, "function Get-PresentMonProcessSnapshot(", "\nfunction Get-PresentMonLiveProcessId(")
        # r3: every process is enumerated (no -Name filter), and the command lines are read with -ErrorAction Stop too
        self.assertIn("foreach ($candidate in @(Get-Process -ErrorAction Stop)) {", snapshot)
        self.assertIn("Get-CimInstance -ClassName Win32_Process -Property ProcessId, CommandLine -ErrorAction Stop", snapshot)
        self.assertNotIn("SilentlyContinue", snapshot, "an unreadable list must never read as 'no capture alive'")

    def test_the_pinned_image_name_drops_only_the_exe_extension_of_the_real_pinned_name(self) -> None:
        # the job derives the image name by regex (the publish-write scan allowlists no static member): run the job's own pattern on the real pinned name and on an extension-less one
        pattern = re.search(r"\$pinnedName = \(\[string\]\$PresentMonName\) -replace '([^']+)', ''", TEMPLATE)
        self.assertIsNotNone(pattern, "the pinned image name is derived by a -replace in the snapshot function")
        proc = run_pwsh(["-Command", f"('PresentMon-2.5.1-x64.exe' -replace '{pattern.group(1)}', '') + '|' + ('PresentMon-2.5.1-x64' -replace '{pattern.group(1)}', '')"])
        self.assertEqual(proc.stdout.strip(), "PresentMon-2.5.1-x64|PresentMon-2.5.1-x64", proc.stdout + proc.stderr)

    def test_the_snapshot_uses_no_static_member_the_publish_write_scan_would_reject(self) -> None:
        snapshot = _slice(TEMPLATE, "function Get-PresentMonProcessSnapshot(", "\nfunction Get-PresentMonLiveProcessId(")
        self.assertIsNone(re.search(r"\[[A-Za-z.]+\]::", snapshot), "R4 allowlists static members; the snapshot must use operators only")

    def test_mutation_without_the_service_lookup_the_service_blocks_the_sweep_again(self) -> None:
        mutated = mutate(TEMPLATE, "if ($registered.Count -gt 0) {", "if ($false) {")
        _code, sweep, d, _out = self.run_start("mut-no-service", others=(self.SERVICE_UNREADABLE,), services=self.SERVICE_ROW, text=mutated)
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive", "the registered service reads as unidentified: nothing is swept")
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_mutation_an_unreadable_path_read_as_not_live_sweeps_under_an_unidentified_process(self) -> None:
        mutated = mutate(TEMPLATE, "if (-not $pathReadable) { $reason = 'unreadable_path_not_a_registered_service' }", "if (-not $pathReadable) { $serviceName = $null }")
        _code, sweep, d, _out = self.run_start("mut-unreadable", others=(self.SERVICE_UNREADABLE,), text=mutated)
        self.assertTrue(sweep["ran"], "sol's repro: ran=true, error=null, the sweep terminates under an unidentified PresentMon")
        self.assertIn("PresentMon", self.terminated_names(self.calls(d)), "the default session is terminated under an unidentified PresentMon")

    def test_mutation_a_failing_service_lookup_read_as_no_service_is_not_fail_closed(self) -> None:
        mutated = mutate(TEMPLATE, "Get-CimInstance -ClassName Win32_Service -Filter \"ProcessId = $candidateId\" -ErrorAction Stop", "Get-CimInstance -ClassName Win32_Service -Filter \"ProcessId = $candidateId\" -ErrorAction SilentlyContinue")
        _code, sweep, d, _out = self.run_start("mut-lookup", others=((4950, "PresentMonOverlayHelper", "C:\\Program Files\\Vendor\\overlay.exe"),), service_lookup_fails=True, text=mutated)
        # r2: the readable-path process is live either way, so the mutation no longer sweeps; what it loses is the evidence -- the failed lookup no longer reaches `error`
        self.assertIsNone(sweep["error"], "the mutation reads a failed lookup as 'not a service' silently")
        _code, sweep, _d, _out = self.run_start("unmut-lookup", others=((4950, "PresentMonOverlayHelper", "C:\\Program Files\\Vendor\\overlay.exe"),), service_lookup_fails=True)
        self.assertIn("stub Get-CimInstance failure", sweep["error"])

    def test_a_path_getter_that_throws_reads_as_unreadable_not_as_an_error(self) -> None:
        # measured on pwsh 7.6: PowerShell turns a throwing .NET property getter into $null (so the job's try/catch around .Path is defence in depth and removing it changes nothing a test can see);
        # what matters, and is pinned here, is that the unreadable path is recorded as pathReadable=false and the process stays live unless it is a registered service
        code, sweep, d, out = self.run_start("getter-throws", others=((4361, "PresentMon-2.4.1-x64", UNREADABLE),))
        self.assertEqual(code, 0, out)
        self.assertIsNone(sweep["error"])
        self.assertEqual(sweep["livePresentMonProcesses"], [{"name": "PresentMon-2.4.1-x64", "pid": 4361, "pathReadable": False, "reason": "unreadable_path_not_a_registered_service",
                                                             "nonCaptureIdentity": None, "parentName": None, "parentPid": None, "identityError": None}])

    def test_mutation_a_readable_foreign_capture_image_loses_only_its_reason_label(self) -> None:
        # r2: a readable path is no identity, so the capture-image arm decides the recorded reason only; the foreign capture stays live and the sweep stays off it
        mutated = mutate(TEMPLATE, "elseif ($candidateName -imatch $captureImagePattern) { $reason = 'capture_image_name' }", "elseif ($false) { $reason = 'capture_image_name' }")
        _code, sweep, d, _out = self.run_start("mut-capture-image", others=((4901, "PresentMon-2.4.1-x64", "C:\\tools\\PresentMon-2.4.1-x64.exe"),), text=mutated)
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["livePresentMonProcesses"][0]["reason"], "readable_path_not_a_registered_service")
        self.assertEqual(self.terminated_names(self.calls(d)), [])

    def test_mutation_a_readable_unidentified_process_read_as_excluded_sweeps_under_a_second_harness(self) -> None:
        # the base rule, restored: a readable path that is no capture image was excluded, so scope all stopped the other harness's live session
        mutated = mutate(TEMPLATE, "else { $reason = 'readable_path_not_a_registered_service' }", "else { $reason = $null }")
        _code, sweep, d, _out = self.run_start("mut-readable-excluded", others=(self.CUSTOM_READABLE,), text=mutated)
        self.assertTrue(sweep["ran"])
        self.assertIn("MLVAttr3-old-killed-job", self.terminated_names(self.calls(d)))

    def test_mutation_a_name_prefix_match_lets_every_presentmon_star_process_be_live(self) -> None:
        # the converse of the Bachelor bug: a registered service must not be recognised by name alone, so the name arm is the pinned name only
        mutated = mutate(TEMPLATE, "if ($candidateName -ieq $pinnedName) {", "if ($candidateName -like 'PresentMon*') {")
        _code, sweep, d, _out = self.run_start("mut-prefix", others=(self.SERVICE,), services=self.SERVICE_ROW, text=mutated)
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["livePresentMonProcessIds"], [7001])

    def test_mutation_without_the_path_match_a_pinned_path_copy_reads_as_no_capture(self) -> None:
        mutated = mutate(TEMPLATE, "elseif ($pathReadable -and $candidatePath -ieq $pinnedPath) { $reason = 'pinned_image_path' }", "elseif ($false) { $reason = 'pinned_image_path' }")
        _code, sweep, d, _out = self.run_start("mut-path", others=((4343, "PresentMon-renamed", str(self.stmp / "mut-path" / "cache" / STUB_EXE)),), text=mutated)
        # r2: the copy stays live through the fail-closed readable-path arm; without the path arm the record loses the evidence that it IS the pinned binary
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["livePresentMonProcesses"][0]["reason"], "readable_path_not_a_registered_service")

    def test_mutation_a_case_sensitive_name_match_misses_the_pinned_image(self) -> None:
        stub = Path(STUB_EXE).stem
        mutated = mutate(TEMPLATE, "if ($candidateName -ieq $pinnedName) {", "if ($candidateName -ceq $pinnedName) {")
        _code, sweep, d, _out = self.run_start("mut-case", others=((4402, stub.upper(), "C:\\Vendor\\copy.exe"),), text=mutated)
        self.assertFalse(sweep["ran"], "r2: still live (no identity), so still never swept under")
        self.assertEqual(sweep["livePresentMonProcesses"][0]["reason"], "readable_path_not_a_registered_service", "the mutation no longer recognises the pinned image")

    def test_mutation_without_the_name_match_a_pinned_capture_with_an_aliased_path_is_not_recognised(self) -> None:
        stub = Path(STUB_EXE).stem
        mutated = mutate(TEMPLATE, "if ($candidateName -ieq $pinnedName) { $reason = 'pinned_image_name' }", "if ($false) { $reason = 'pinned_image_name' }")
        _code, sweep, d, _out = self.run_start("mut-alias", others=((4360, stub, "C:\\Users\\runneradmin\\AppData\\Local\\Temp\\cache\\" + STUB_EXE),), text=mutated)
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["livePresentMonProcesses"][0]["reason"], "readable_path_not_a_registered_service",
                         "the path arm alone cannot match a path spelled another way (8.3 versus long); only the name arm records it as the pinned image")

    def test_mutation_without_the_excluded_record_the_service_leaves_no_evidence(self) -> None:
        mutated = mutate(TEMPLATE, "$record['excludedPresentMonProcesses'] = @($snapshot.excluded)", "$null = $snapshot")
        _code, sweep, _d, _out = self.run_start("mut-excluded", others=(self.SERVICE,), services=self.SERVICE_ROW, text=mutated)
        self.assertEqual(sweep["excludedPresentMonProcesses"], [])

    def test_mutation_without_the_path_readable_flag_the_record_cannot_tell_an_unreadable_path(self) -> None:
        mutated = mutate(TEMPLATE, "service = $serviceName; pathReadable = $pathReadable;", "service = $serviceName; pathReadable = $true;")
        _code, sweep, _d, _out = self.run_start("mut-readable", others=(self.SERVICE_UNREADABLE,), services=self.SERVICE_ROW, text=mutated)
        self.assertTrue(sweep["excludedPresentMonProcesses"][0]["pathReadable"], "the mutation reports an unreadable path as readable")
        _code, sweep, _d, _out = self.run_start("unmut-readable", others=(self.SERVICE_UNREADABLE,), services=self.SERVICE_ROW)
        self.assertFalse(sweep["excludedPresentMonProcesses"][0]["pathReadable"])

    def test_the_candidate_record_never_carries_a_path(self) -> None:
        _code, sweep, d, _out = self.run_start("no-path-text", others=(self.SERVICE, (4901, "PresentMon-2.4.1-x64", "C:\\tools\\PresentMon-2.4.1-x64.exe")), services=self.SERVICE_ROW)
        text = json.dumps(sweep) + (d / "trace.log").read_text(encoding="utf-8")
        self.assertNotIn("Program Files", text)
        self.assertNotIn("C:\\\\tools", text)
        self.assertNotIn("C:\\tools", text)

    def test_a_presentmon_that_appears_while_the_listing_runs_is_never_swept(self) -> None:
        code, sweep, d, out = self.run_start("late-live", live_pids=(4242,), live_from_second_check=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["skippedReason"], "presentmon_process_started_during_sweep")
        self.assertEqual(sweep["livePresentMonProcessIds"], [4242])

    def test_a_host_with_no_orphan_runs_the_sweep_and_touches_nothing(self) -> None:
        code, sweep, d, out = self.run_start("clean", sessions=("Circular Kernel Context Logger",))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertTrue(sweep["ran"])
        self.assertEqual((sweep["listed"], sweep["actions"], sweep["remaining"]), ([], [], []))

    def test_logman_stop_runs_only_for_a_session_still_listed_after_the_presentmon_terminate(self) -> None:
        code, sweep, d, out = self.run_start("fallback", helper_works=False, logman_stop_works=True)
        self.assertEqual(code, 0, out)
        stopped = [l.strip() for l in (d / "stopped.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(stopped, ["PresentMon", "MLVAttr3-old-killed-job"])
        methods = [(a["session"], a["method"], a["exitCode"]) for a in sweep["actions"]]
        self.assertEqual(methods, [("PresentMon", "presentmon_terminate", 1), ("MLVAttr3-old-killed-job", "presentmon_terminate", 1),
                                   ("PresentMon", "logman_stop", 0), ("MLVAttr3-old-killed-job", "logman_stop", 0)])
        self.assertEqual(sweep["remaining"], [])

    def test_a_successful_terminate_never_needs_logman_stop(self) -> None:
        _code, sweep, d, _out = self.run_start("no-fallback")
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")
        self.assertNotIn("logman_stop", [a["method"] for a in sweep["actions"]])

    def test_a_session_that_survives_both_is_recorded_as_remaining_and_the_capture_still_starts(self) -> None:
        code, sweep, d, out = self.run_start("stubborn", helper_works=False, logman_stop_works=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(sweep["remaining"], ["PresentMon", "MLVAttr3-old-killed-job"], "an orphan that could not be removed is evidence, not silence")
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1)
        self.assertIn("remaining", (d / "trace.log").read_text(encoding="utf-8"))

    def test_a_sweep_that_fails_is_recorded_and_never_blocks_the_capture(self) -> None:
        code, sweep, d, out = self.run_start("sweep-throws", get_process_throws=True)
        self.assertEqual(code, 0, out)
        self.assertIn("stub Get-Process failure", sweep["error"])
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1)

    def test_a_logman_listing_that_fails_is_recorded_and_terminates_nothing(self) -> None:
        code, sweep, d, out = self.run_start("query-fails", logman_query_fails=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertTrue(sweep["error"] or sweep["listError"], sweep)
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1)

    # --- r2 (sol blocker 1, fable UM-SWEEP-FALLBACK-SCOPE-1): the sweep acts only on names it listed before its second liveness check, and re-checks liveness before every action
    def test_the_fallback_never_stops_a_session_that_was_not_in_the_orphan_list(self) -> None:
        # sol's repro: no candidate at the scan; another capture starts and its MLVAttr3-* session is in the NEXT listing (the one the fallback reads)
        code, sweep, d, out = self.run_start("late-session", sessions=("Circular Kernel Context Logger",), extra_from_query=2)
        self.assertEqual(code, 0, out)
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "", "a session the scan did not list is never stopped")
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertEqual(sweep["actions"], [])
        self.assertEqual(sweep["remaining"], [], "nothing the scan listed is left")
        self.assertEqual(sweep["newlyListed"], ["MLVAttr3-another-live-job"], "what appeared after the scan is evidence, never a target")

    def test_the_fallback_leaves_a_session_that_appeared_after_the_scan_even_beside_a_real_orphan(self) -> None:
        code, sweep, d, out = self.run_start("late-beside-orphan", sessions=("PresentMon",), helper_works=False, extra_from_query=2)
        self.assertEqual(code, 0, out)
        self.assertEqual([l.strip() for l in (d / "stopped.txt").read_text(encoding="utf-8").splitlines() if l.strip()], ["PresentMon"])
        self.assertEqual(sweep["newlyListed"], ["MLVAttr3-another-live-job"])

    def test_a_presentmon_that_appears_after_one_terminate_stops_the_sweep_before_the_next_one(self) -> None:
        code, sweep, d, out = self.run_start("live-mid-terminate", live_after_terminate_of="PresentMon")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["PresentMon"], "the second orphan is not touched once a PresentMon process is alive")
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")
        self.assertEqual(sweep["abortedReason"], "presentmon_process_started_during_sweep")
        self.assertEqual(sweep["abortedBefore"], "MLVAttr3-old-killed-job:presentmon_terminate")
        self.assertEqual(sweep["livePresentMonProcessIds"], [5151])
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1, "the capture itself still starts")
        self.assertIn("presentmon_process_started_during_sweep", (d / "trace.log").read_text(encoding="utf-8"))

    def test_a_presentmon_that_appears_after_the_terminates_stops_the_sweep_before_any_logman_stop(self) -> None:
        code, sweep, d, out = self.run_start("live-before-stop", helper_works=False, live_after_terminate_of="MLVAttr3-old-killed-job")
        self.assertEqual(code, 0, out)
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "", "logman stop is never issued under a live PresentMon")
        self.assertEqual(sweep["abortedReason"], "presentmon_process_started_during_sweep")
        self.assertEqual(sweep["abortedBefore"], "PresentMon:logman_stop")
        self.assertEqual(sweep["remaining"], ["PresentMon", "MLVAttr3-old-killed-job"], "what was left is recorded")

    def test_mutation_without_the_orphan_list_scope_the_fallback_stops_another_jobs_session(self) -> None:
        mutated = mutate(TEMPLATE, "$stillListed = @($listed | Where-Object { $afterTerminateNames -contains $_ })", "$stillListed = @($afterTerminateNames)")
        _code, _sweep, d, _out = self.run_start("mut-scope", sessions=("Circular Kernel Context Logger",), extra_from_query=2, text=mutated)
        self.assertIn("MLVAttr3-another-live-job", (d / "stopped.txt").read_text(encoding="utf-8"), "sol's repro: the live capture's session is stopped")

    def test_mutation_without_the_per_terminate_liveness_check_the_sweep_goes_on_under_a_live_capture(self) -> None:
        mutated = mutate(TEMPLATE, "if ($liveBeforeTerminate.Count -gt 0) {", "if ($false) {")
        _code, _sweep, d, _out = self.run_start("mut-term-live", live_after_terminate_of="PresentMon", text=mutated)
        self.assertEqual(self.terminated_names(self.calls(d)), ["PresentMon", "MLVAttr3-old-killed-job"])

    def test_mutation_without_the_per_stop_liveness_check_logman_stops_under_a_live_capture(self) -> None:
        mutated = mutate(TEMPLATE, "if ($liveBeforeStop.Count -gt 0) {", "if ($false) {")
        _code, _sweep, d, _out = self.run_start("mut-stop-live", helper_works=False, live_after_terminate_of="MLVAttr3-old-killed-job", text=mutated)
        self.assertNotEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")

    # --- r2 hardening: a wide name, a transient listing failure, a middle listing that fails, a logman that stalls
    def test_a_wide_session_name_in_the_real_layout_is_terminated_by_the_executed_sweep(self) -> None:
        wide = ModuleFunctionTests.WIDE_JOB_SESSION
        code, sweep, d, out = self.run_start("wide", sessions=("PresentMon", wide))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), ["PresentMon", wide])
        self.assertEqual((sweep["listed"], sweep["remaining"]), (["PresentMon", wide], []))

    def test_a_transient_listing_failure_is_retried_once(self) -> None:
        code, sweep, d, out = self.run_start("retry", query_exits={1: 1})
        self.assertEqual(code, 0, out)
        self.assertTrue(sweep["ran"], sweep)
        self.assertEqual(sweep["listAttempts"], 2)
        self.assertEqual(self.terminated_names(self.calls(d)), ["PresentMon", "MLVAttr3-old-killed-job"])
        self.assertIsNone(sweep["listError"])

    def test_a_listing_that_fails_twice_is_recorded_after_two_attempts_and_terminates_nothing(self) -> None:
        code, sweep, d, out = self.run_start("retry-twice", query_exits={1: 1, 2: 1})
        self.assertEqual(code, 0, out)
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["listAttempts"], 2)
        self.assertIn("attempts=2", sweep["listError"])
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1)

    def test_a_failed_middle_listing_is_a_recorded_verification_failure(self) -> None:
        # sol's repro: the query after the terminates fails with an exit code and no exception text; the survivor must not read as a clean listing
        code, sweep, _d, out = self.run_start("middle-fails", query_exits={2: 5, 3: 5})
        self.assertEqual(code, 0, out)
        self.assertIn("afterTerminate exit=5", sweep["listError"])

    def test_a_logman_that_stalls_is_cut_off_and_recorded_and_the_capture_still_starts(self) -> None:
        code, sweep, d, out = self.run_start("hang-query", logman_hangs=True, logman_timeout_seconds=1)
        self.assertEqual(code, 0, out)
        self.assertIn("did not exit within 1 s", sweep["listError"])
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1)

    def test_a_logman_stop_that_stalls_is_cut_off_and_recorded(self) -> None:
        # the deadline must exceed a pwsh stub's own start-up, which the query before the stop also pays
        code, sweep, d, out = self.run_start("hang-stop", helper_works=False, logman_stop_hangs=True, logman_timeout_seconds=4)
        self.assertEqual(code, 0, out)
        stops = [a for a in sweep["actions"] if a["method"] == "logman_stop"]
        self.assertEqual([(a["session"], a["timedOut"]) for a in stops], [("PresentMon", True), ("MLVAttr3-old-killed-job", True)])
        self.assertEqual(sweep["remaining"], ["PresentMon", "MLVAttr3-old-killed-job"])
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1)

    def test_mutation_without_the_listing_retry_a_transient_failure_skips_the_sweep(self) -> None:
        mutated = mutate(TEMPLATE, "Start-Sleep -Milliseconds 500", "return [pscustomobject]@{ exitCode = $listing.exitCode; text = $listing.text; error = $listing.error; timedOut = $listing.timedOut; attempts = 1 }")
        _code, sweep, d, _out = self.run_start("mut-retry", query_exits={1: 1}, text=mutated)
        self.assertFalse(sweep["ran"])
        self.assertEqual(self.terminated_names(self.calls(d)), [], "the UM state: one transient listing error and the orphan stays")

    def test_mutation_without_the_middle_listing_exit_code_the_failed_query_is_unrecorded(self) -> None:
        mutated = mutate(TEMPLATE, "$null -ne $afterTerminate.error -or $afterTerminate.exitCode -ne 0 -or ", "$null -ne $afterTerminate.error -or ")
        _code, sweep, _d, _out = self.run_start("mut-predicate", query_exits={2: 5, 3: 5}, text=mutated)
        self.assertIsNone(sweep["listError"])

    def test_the_job_hands_every_logman_wrapper_its_deadline(self) -> None:
        sweep = _slice(TEMPLATE, "function Get-PresentMonEtsListing(", "\nfunction Split-PresentMonCsvLine(")
        self.assertEqual(sweep.count("-TimeoutSeconds (Get-PresentMonLogmanTimeoutSecond)"), 3, "two listing attempts and the stop")
        self.assertEqual(sweep.count("Get-AttrCudaEtsSessionListing -LogmanPath"), 2)
        self.assertEqual(sweep.count("Stop-AttrCudaEtsSession -SessionName $name -LogmanPath"), 1)

    def test_without_the_callers_record_table_nothing_is_swept(self) -> None:
        # the table is the opt-in: the other suites that execute Start-PresentMonCapture never reach a real logman or session
        code, _sweep, d, out = self.run_start("not-enabled", enabled=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")

    def test_the_job_creates_the_record_table_before_it_starts_the_capture(self) -> None:
        init = TEMPLATE.index("$presentMonOrphanSweep = [ordered]@{")
        self.assertLess(init, TEMPLATE.index("$presentMonProc = Start-PresentMonCapture $presentMonPath"))

    def test_mutation_without_the_sweep_the_orphan_survives_again(self) -> None:
        mutated = mutate(TEMPLATE, "$sweepResult = Invoke-PresentMonOrphanSweep", "$sweepResult = [ordered]@{ ran = $false }")
        code, _sweep, d, out = self.run_start("mut-nosweep", text=mutated)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [], "the UM state of 2026-10-03: the orphan stays and starves the next capture")

    def test_mutation_without_the_live_process_guard_a_live_capture_is_swept(self) -> None:
        # three guards (before the listing, again right after it, and before each terminate -- the stop's own is pinned in the r2 tests): each mutation alone leaves the others standing,
        # so all are removed together and then each singly
        both = mutate(mutate(mutate(TEMPLATE, "if ($live.Count -gt 0) {", "if ($false) {"), "if ($liveNow.Count -gt 0) {", "if ($false) {"), "if ($liveBeforeTerminate.Count -gt 0) {", "if ($false) {")
        _code, _sweep, d, _out = self.run_start("mut-guard", live_pids=(4242,), text=both)
        self.assertNotEqual(self.terminated_names(self.calls(d)), [], "the mutation sweeps under a live PresentMon -- the guards are what stop it")
        first_only = mutate(TEMPLATE, "if ($live.Count -gt 0) {", "if ($false) {")
        _code, sweep, d2, _out = self.run_start("mut-guard-first", live_pids=(4242,), live_from_second_check=False, text=first_only)
        self.assertEqual(self.terminated_names(self.calls(d2)), [], "the second guard still holds when the first is gone")
        self.assertEqual(sweep["skippedReason"], "presentmon_process_started_during_sweep")
        second_only = mutate(mutate(TEMPLATE, "if ($liveNow.Count -gt 0) {", "if ($false) {"), "if ($liveBeforeTerminate.Count -gt 0) {", "if ($false) {")
        _code, _sweep, d3, _out = self.run_start("mut-guard-second", live_pids=(4242,), live_from_second_check=True, text=second_only)
        self.assertNotEqual(self.terminated_names(self.calls(d3)), [], "without the second guard a capture that starts during the listing is swept")

    def test_mutation_without_the_record_the_sweep_leaves_no_evidence(self) -> None:
        mutated = mutate(TEMPLATE, "$presentMonOrphanSweep[$key] = $sweepResult[$key]", "$null = $key")
        _code, sweep, _d, _out = self.run_start("mut-record", text=mutated)
        self.assertEqual(sweep, {})

    def test_mutation_without_the_logman_fallback_a_stubborn_session_is_never_stopped(self) -> None:
        mutated = mutate(TEMPLATE, "$stopResult = Stop-AttrCudaEtsSession -SessionName $name -LogmanPath ([string]$presentMonLogmanPath) -TimeoutSeconds (Get-PresentMonLogmanTimeoutSecond)",
                         "$stopResult = [pscustomobject]@{ exitCode = 1; error = $null }")
        _code, sweep, d, _out = self.run_start("mut-fallback", helper_works=False, text=mutated)
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")
        self.assertEqual(sweep["remaining"], ["PresentMon", "MLVAttr3-old-killed-job"])


# --------------------------------------------------------------------------------------------------------------------- kill_fallback
@requires_windows_pwsh
class KillFallbackTerminatesOwnSessionTests(SliceHarness, unittest.TestCase):
    """Item 2: after a job-issued Kill() the job's own named session is terminated, in Wait-PresentMonCapture and in Stop-PresentMonCapture."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def run_stop(self, name: str, call: str, terminate_ends_process: bool, text: str | None = None) -> tuple[int, dict, list[str], str]:
        d = self.stmp / name
        d.mkdir(parents=True)
        calls = d / "calls.log"
        calls.write_text("", encoding="utf-8")
        body = text if text is not None else TEMPLATE
        ends = "Stop-Process -Id $global:capturePid -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 300; " if terminate_ends_process else ""
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            "$Cache = 'C:\\no-such-cache'\n$PresentMonName = 'PresentMon-2.5.1-x64.exe'\n"
            + presentmon_functions(body) + "\n"
            f"$callsFile = '{ps(calls)}'\n"
            "function Invoke-PresentMonSessionTerminate([string]$SessionName, [int]$TimeoutSeconds = 10) {\n"
            "    Add-Content -LiteralPath $callsFile -Value \"terminate $SessionName\"\n"
            f"    {ends}[pscustomobject]@{{ exitCode = {'0' if terminate_ends_process else '1'}; timedOut = $false; error = $null }}\n"
            "}\n"
            "$p = Start-Process -FilePath 'ping.exe' -ArgumentList @('-n', '120', '127.0.0.1') -PassThru -WindowStyle Hidden\n"
            "$global:capturePid = $p.Id\n"
            "try {\n"
            f"    $r = {call}\n"
            "    Write-Output ('RESULT=' + ($r | ConvertTo-Json -Depth 6 -Compress))\n"
            "} finally { if (-not $p.HasExited) { $p.Kill() } }\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], timeout=180)
        result: dict = {}
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT="):
                result = json.loads(line[len("RESULT="):])
        return proc.returncode, result, [l.strip() for l in calls.read_text(encoding="utf-8").splitlines() if l.strip()], proc.stdout + proc.stderr

    WAIT_CALL = "Wait-PresentMonCapture $p -SessionName 'MLVAttr3-the-job' -TimeoutSeconds 1 -KillWaitTimeoutSeconds 10"
    STOP_CALL = "Stop-PresentMonCapture $p -TimeoutSeconds 10 -SessionName 'MLVAttr3-the-job'"

    def test_wait_terminates_the_own_session_again_after_its_kill_fallback(self) -> None:
        code, result, calls, out = self.run_stop("wait-kill", self.WAIT_CALL, terminate_ends_process=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(result["stopMethod"], "kill_fallback")
        self.assertEqual(calls, ["terminate MLVAttr3-the-job", "terminate MLVAttr3-the-job"], "one terminate before the Kill(), one after it")
        self.assertTrue(result["postKillTerminateIssued"])
        self.assertEqual(result["postKillTerminateExitCode"], 1)
        self.assertFalse(result["postKillTerminateTimedOut"])

    def test_wait_without_a_kill_issues_no_second_terminate(self) -> None:
        code, result, calls, out = self.run_stop("wait-clean", self.WAIT_CALL, terminate_ends_process=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(result["stopMethod"], "session_terminate")
        self.assertEqual(calls, ["terminate MLVAttr3-the-job"])
        self.assertFalse(result["postKillTerminateIssued"])
        self.assertIsNone(result["postKillTerminateExitCode"])

    def test_stop_terminates_the_own_session_again_after_its_kill(self) -> None:
        code, result, calls, out = self.run_stop("stop-kill", self.STOP_CALL, terminate_ends_process=False)
        self.assertEqual(code, 0, out)
        self.assertTrue(result["confirmedExited"])
        self.assertEqual(calls, ["terminate MLVAttr3-the-job", "terminate MLVAttr3-the-job"])
        self.assertEqual(result["postKillSessionTerminate"]["exitCode"], 1)

    def test_stop_without_a_kill_issues_no_second_terminate(self) -> None:
        code, result, calls, out = self.run_stop("stop-clean", self.STOP_CALL, terminate_ends_process=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(calls, ["terminate MLVAttr3-the-job"])
        self.assertIsNone(result["postKillSessionTerminate"])

    def test_mutation_without_the_wait_post_kill_terminate_the_session_orphans_again(self) -> None:
        mutated = mutate(TEMPLATE, "$postKillTerminate = Invoke-PresentMonSessionTerminate -SessionName $SessionName -TimeoutSeconds 5", "$postKillTerminate = $null")
        _code, _result, calls, _out = self.run_stop("mut-wait", self.WAIT_CALL, terminate_ends_process=False, text=mutated)
        self.assertEqual(calls, ["terminate MLVAttr3-the-job"], "the inferred UM state: a killed capture's own session stays")

    def test_mutation_without_the_stop_post_kill_terminate_the_session_orphans_again(self) -> None:
        mutated = mutate(TEMPLATE, "$postKillSessionTerminate = Invoke-PresentMonSessionTerminate -SessionName $SessionName -TimeoutSeconds 5", "$postKillSessionTerminate = $null")
        _code, _result, calls, _out = self.run_stop("mut-stop", self.STOP_CALL, terminate_ends_process=False, text=mutated)
        self.assertEqual(calls, ["terminate MLVAttr3-the-job"])


# --------------------------------------------------------------------------------------------------------------------- the post-Kill terminate is published (r2, sol blocker 2)
KEEPALIVE_START = "$keepAliveHealthBeforeSmokeLaunch = Get-AttrCudaDisplayWakeKeepAliveHealth"
KEEPALIVE_END = "\n$smokeRc = $null"
SMOKE_FAILED_START = "if ($null -ne $smokeLaunchException -or $smokeRc -ne 0 -or -not (Test-Path -LiteralPath $resultPath)) {"
SMOKE_FAILED_END = "\n# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 1 / fable HARDENING): the smoke run's own"
LOG_UNAVAILABLE_START = "try {\n    $runLog = Resolve-AttrCudaSmokeRunLog -ResultJsonPath $resultPath -ContainingRoot $Work\n} catch {"
LOG_UNAVAILABLE_END = "\n$logPath = $runLog.path"
POSTKILL_FIELD = "presentMonPostKillSessionTerminate"


@requires_windows_pwsh
class PostKillTerminateIsPublishedTests(SliceHarness, unittest.TestCase):
    """Item 2 of the evidence class: a failed post-Kill session terminate is visible on the job that caused it -- in the summary of every terminal that stops the capture through
    Stop-PresentMonCapture, and in the PRESENTMON_TIMEOUT reason -- not only through the NEXT job's sweep. The REAL Stop-PresentMonCapture runs (a real child process killed by it)
    with the terminate helper stubbed to fail (exit 1), as in sol's repro."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def run_terminal(self, name: str, block: str, preamble: str, expect_exit: int, text: str | None = None) -> tuple[int, dict, str]:
        d = self.stmp / name
        pub = d / "pub"
        pub.mkdir(parents=True)
        body = text if text is not None else TEMPLATE
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            "$Cache = 'C:\\no-such-cache'\n$PresentMonName = 'PresentMon-2.5.1-x64.exe'\n"
            + presentmon_functions(body) + "\n"
            "function Invoke-PresentMonSessionTerminate([string]$SessionName, [int]$TimeoutSeconds = 10) { [pscustomobject]@{ exitCode = 1; timedOut = $false; error = 'stub: the session survived' } }\n"
            "function Save-Json($Object, [string]$Path) { [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30)) }\n"
            "function Write-JobTrace([string]$Message) { Add-Content -LiteralPath '" + ps(d / "trace.log") + "' -Value $Message }\n"
            f"$Pub = '{ps(pub)}'\n$FixtureRehearsal = $false\n$SourceCommit = ('1' * 40)\n$ClipId = '{OWNER_CLIP}'\n"
            "$PresentMonSessionName = 'MLVAttr3-the-job'\n$displayWake = [ordered]@{}\n$displayBlock = [ordered]@{ venue = 'ultra-magnus' }\n"
            "$presentMonProc = Start-Process -FilePath 'ping.exe' -ArgumentList @('-n', '120', '127.0.0.1') -PassThru -WindowStyle Hidden\n"
            + preamble + "\n"
            "try {\n" + block + "\n} finally { if (-not $presentMonProc.HasExited) { $presentMonProc.Kill() } }\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], timeout=180)
        summary_path = pub / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        trace = (d / "trace.log").read_text(encoding="utf-8") if (d / "trace.log").exists() else ""
        self.assertEqual(proc.returncode, expect_exit, proc.stdout + proc.stderr)
        return proc.returncode, summary, trace

    def keepalive(self, name: str, text: str | None = None) -> tuple[int, dict, str]:
        body = text if text is not None else TEMPLATE
        return self.run_terminal(name, _slice(body, KEEPALIVE_START, KEEPALIVE_END),
                                 "function Get-AttrCudaDisplayWakeKeepAliveHealth($Handle) { [pscustomobject]@{ healthy = $false; reason = 'stub keep-alive failure' } }\n$displayWakeKeepAlive = $null", 26, text)

    def smoke_failed(self, name: str, text: str | None = None) -> tuple[int, dict, str]:
        body = text if text is not None else TEMPLATE
        return self.run_terminal(name, _slice(body, SMOKE_FAILED_START, SMOKE_FAILED_END),
                                 "$legOut = Join-Path $Pub 'legOut'; [void](New-Item -ItemType Directory -Path $legOut)\n$smokeRc = 1; $smokeLaunchException = $null; $resultPath = Join-Path $legOut 'result.json'\n"
                                 "$Work = $Pub; $smokeLaunchedAtUtc = [datetime]::UtcNow\n"
                                 "function Find-AttrCudaFailedSmokeDisplayLog($LegOut, $LaunchedAtUtc) { [pscustomobject]@{ found = $false; logPath = $null; reason = 'stub' } }", 18, text)

    def log_unavailable(self, name: str, text: str | None = None) -> tuple[int, dict, str]:
        body = text if text is not None else TEMPLATE
        return self.run_terminal(name, _slice(body, LOG_UNAVAILABLE_START, LOG_UNAVAILABLE_END),
                                 "function Resolve-AttrCudaSmokeRunLog($ResultJsonPath, $ContainingRoot) { throw 'stub: no run log' }\n$resultPath = Join-Path $Pub 'result.json'; $Work = $Pub; $smokeRc = 0", 16, text)

    def assert_published(self, summary: dict, trace: str) -> None:
        self.assertIn(POSTKILL_FIELD, summary, "the terminal drops the post-Kill terminate outcome")
        self.assertEqual(summary[POSTKILL_FIELD]["exitCode"], 1)
        self.assertEqual(summary[POSTKILL_FIELD]["error"], "stub: the session survived")
        self.assertTrue(summary["presentMonConfirmedExited"], "the Kill itself succeeded: only the session cleanup failed")
        self.assertIn("postKillTerminate", trace, "and the trace says so")

    def test_the_keepalive_terminal_publishes_a_failed_post_kill_terminate(self) -> None:
        _code, summary, trace = self.keepalive("keepalive")
        self.assertEqual(summary["result"], "KEEPALIVE_FAILED")
        self.assert_published(summary, trace)

    def test_the_smoke_run_failed_terminal_publishes_a_failed_post_kill_terminate(self) -> None:
        _code, summary, trace = self.smoke_failed("smoke-failed")
        self.assertEqual(summary["result"], "SMOKE_RUN_FAILED")
        self.assert_published(summary, trace)

    def test_the_smoke_log_unavailable_terminal_publishes_a_failed_post_kill_terminate(self) -> None:
        _code, summary, trace = self.log_unavailable("log-unavailable")
        self.assertEqual(summary["result"], "SMOKE_LOG_UNAVAILABLE")
        self.assert_published(summary, trace)

    def test_a_terminal_whose_capture_needed_no_kill_publishes_no_post_kill_outcome(self) -> None:
        # the capture exits when its session is terminated (the stop never reaches the Kill): nothing is claimed
        d = self.stmp / "no-kill"
        text = TEMPLATE
        _code, summary, _trace = self.run_terminal("no-kill", _slice(text, KEEPALIVE_START, KEEPALIVE_END),
                                                   "function Get-AttrCudaDisplayWakeKeepAliveHealth($Handle) { [pscustomobject]@{ healthy = $false; reason = 'stub keep-alive failure' } }\n$displayWakeKeepAlive = $null\n"
                                                   "$presentMonProc.Kill(); [void]$presentMonProc.WaitForExit(5000)", 26)
        self.assertIsNone(summary.get(POSTKILL_FIELD))
        self.assertTrue(d.exists())

    def test_mutation_without_the_keepalive_field_the_failed_cleanup_is_unpublished_again(self) -> None:
        mutated = mutate(TEMPLATE, "presentMonPostKillSessionTerminate=$presentMonStopOnKeepAliveFailure.postKillSessionTerminate", "presentMonConfirmedExitedAgain=$presentMonStopOnKeepAliveFailure.confirmedExited")
        _code, summary, _trace = self.keepalive("mut-keepalive", text=mutated)
        self.assertNotIn(POSTKILL_FIELD, summary)

    def test_mutation_without_the_smoke_run_failed_field_the_failed_cleanup_is_unpublished_again(self) -> None:
        mutated = mutate(TEMPLATE, "presentMonPostKillSessionTerminate=$presentMonStop.postKillSessionTerminate\n        # UM-PRESENTMON-ORPHAN-SWEEP-1 <<<\n        # DVE-LEG-TERMINALS-1 >>>\n        smokeEvidence=",
                         "presentMonConfirmedExitedAgain=$presentMonStop.confirmedExited\n        # UM-PRESENTMON-ORPHAN-SWEEP-1 <<<\n        # DVE-LEG-TERMINALS-1 >>>\n        smokeEvidence=")
        _code, summary, _trace = self.smoke_failed("mut-smoke-failed", text=mutated)
        self.assertNotIn(POSTKILL_FIELD, summary)

    def test_mutation_without_the_log_unavailable_field_the_failed_cleanup_is_unpublished_again(self) -> None:
        mutated = mutate(TEMPLATE, "presentMonPostKillSessionTerminate=$presentMonStop.postKillSessionTerminate\n        # UM-PRESENTMON-ORPHAN-SWEEP-1 <<<\n        sourceCommit=",
                         "presentMonConfirmedExitedAgain=$presentMonStop.confirmedExited\n        # UM-PRESENTMON-ORPHAN-SWEEP-1 <<<\n        sourceCommit=")
        _code, summary, _trace = self.log_unavailable("mut-log-unavailable", text=mutated)
        self.assertNotIn(POSTKILL_FIELD, summary)

    def test_mutation_without_the_timeout_detail_the_reason_drops_the_cleanup_again(self) -> None:
        mutated = mutate(TEMPLATE, "$waitErrorText = $waitErrorText + ' postKillTerminate=' + (Format-PresentMonSessionTerminateText $postKillTerminate)", "$null = $postKillTerminate")
        _code, _summary, out = self.wait_timeout("mut-timeout-reason", text=mutated)
        self.assertIn("PRESENTMON_TIMEOUT", out)
        self.assertNotIn("postKillTerminate", out)

    def test_the_timeout_reason_says_when_no_post_kill_terminate_was_issued(self) -> None:
        # the legacy passive wait (no session name) never terminates a session: the reason does not pretend it did
        d = self.stmp / "legacy-timeout"
        d.mkdir(parents=True)
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            "$Cache = 'C:\\no-such-cache'\n$PresentMonName = 'PresentMon-2.5.1-x64.exe'\n"
            + presentmon_functions() + "\n"
            "$p = [pscustomobject]@{ HasExited = $false; ExitCode = $null }\n"
            "Add-Member -InputObject $p -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $false }\n"
            "Add-Member -InputObject $p -MemberType ScriptMethod -Name Kill -Value { }\n"
            "try { [void](Wait-PresentMonCapture $p -TimeoutSeconds 1 -KillWaitTimeoutSeconds 1); Write-Output 'NO_THROW' } catch { Write-Output ('ERR=' + $_.Exception.Message) }\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], timeout=120)
        self.assertIn("postKillTerminate=<not issued>", proc.stdout, proc.stdout + proc.stderr)

    def test_a_timeout_reason_carries_the_post_kill_terminate_outcome(self) -> None:
        # Wait-PresentMonCapture throws PRESENTMON_TIMEOUT when the capture survives the Kill; the reason is all a reader gets of that stop, so it names the post-Kill terminate
        _code, _summary, out = self.wait_timeout("timeout-reason")
        self.assertIn("PRESENTMON_TIMEOUT", out)
        self.assertRegex(out, r"postKillTerminate=exitCode:1 timedOut:False error:stub: the session survived")

    def wait_timeout(self, name: str, text: str | None = None) -> tuple[int, dict, str]:
        d = self.stmp / name
        d.mkdir(parents=True)
        body = text if text is not None else TEMPLATE
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            "$Cache = 'C:\\no-such-cache'\n$PresentMonName = 'PresentMon-2.5.1-x64.exe'\n"
            + presentmon_functions(body) + "\n"
            "function Invoke-PresentMonSessionTerminate([string]$SessionName, [int]$TimeoutSeconds = 10) { [pscustomobject]@{ exitCode = 1; timedOut = $false; error = 'stub: the session survived' } }\n"
            # a capture that survives both the terminate and the Kill()
            "$p = [pscustomobject]@{ HasExited = $false; ExitCode = $null }\n"
            "Add-Member -InputObject $p -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $false }\n"
            "Add-Member -InputObject $p -MemberType ScriptMethod -Name Kill -Value { }\n"
            "try { [void](Wait-PresentMonCapture $p -SessionName 'MLVAttr3-the-job' -TimeoutSeconds 1 -KillWaitTimeoutSeconds 1); Write-Output 'NO_THROW' } catch { Write-Output ('ERR=' + $_.Exception.Message) }\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], timeout=120)
        return proc.returncode, {}, proc.stdout + proc.stderr


# --------------------------------------------------------------------------------------------------------------------- typed evidence in the job
@requires_windows_pwsh
class EventsLostEvidenceInTheJobTests(_StopHarness, unittest.TestCase):
    """Item 3, in the job: PresentMon's stderr saying events were lost is a typed detail in presentmon-capture.json and in the failure terminals' PresentMon reason."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def run_parse_with_events(self, name: str, events_lost_stderr: bytes | None = None, text: str | None = None) -> tuple[int, dict, Path, str]:
        """The real display-report failure branch against the UM-2 shape, after the evidence step: $presentMonEventsLost is what the job's own scan produces from the stderr."""
        d = self.stmp / name
        pub = d / "pub"
        pub.mkdir(parents=True)
        cs = d / "contact-sheet"
        cs.mkdir()
        for fname, data in SIX_FRAMES.items():
            (cs / fname).write_bytes(data)
        (d / "run.log").write_text(um_run_log(UM2_GPU_SUMMARY), encoding="utf-8")
        stderr = d / "presentmon-stderr.txt"
        if events_lost_stderr is not None:
            stderr.write_bytes(events_lost_stderr)
        body = text if text is not None else TEMPLATE
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            + gpu_summary_fn(body) + "\n"
            f"$Pub = '{ps(pub)}'\n$FixtureRehearsal = $false\n$SourceCommit = ('1' * 40)\n$ClipId = '{OWNER_CLIP}'\n"
            "function Save-Json($Object, [string]$Path) { [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30)) }\n"
            "function Write-JobTrace([string]$Message) { }\nfunction Get-AttrCudaDisplayResultTail([object]$DisplayBlock) { '' }\n"
            f"$rawLog = [IO.File]::ReadAllText('{ps(d / 'run.log')}')\n$measuredSmokeSessionId = '1'\n"
            "$gpuSummary = Get-LastGpuSummary $rawLog $measuredSmokeSessionId\n"
            "$gpuFramesTotal = $gpuSummary.gpuReconReadbackFrames + $gpuSummary.gpuTextureReadbackFrames + $gpuSummary.gpuTextureNoReadbackFrames\n"
            f"$presentMonEventsLost = Get-AttrCudaPresentMonEventsLost -StderrPath '{ps(stderr)}'\n"
            f"$displayReport = [pscustomobject]@{{ status = 'PRESENTMON_UNAVAILABLE'; reason = '{UM2_REASON.replace(chr(39), chr(39) * 2)}'; chains = @(); clockBracket = $null }}\n"
            "$displayAsleepOverridden = $false\n$presentMonCaptureStartUtc = [datetime]::UtcNow\n$displayWake = [ordered]@{}\n$displayBlock = [ordered]@{ venue = 'ultra-magnus' }\n"
            "$diagnostics = [ordered]@{}\n$rows = @(1..3)\n$stats = [ordered]@{}\n"
            f"$ContactSheetEnabled = $true\n$contactSheetDir = '{ps(cs)}'\n"
            + _slice(body, PARSE_START, PARSE_END) + "\nWrite-Output 'RESULT=NO_FAILURE_BRANCH_TAKEN'\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        summary_path = pub / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        return proc.returncode, summary, pub, proc.stdout + proc.stderr

    def test_a_wait_failure_carries_the_typed_detail_in_the_capture_json_and_the_reason(self) -> None:
        code, summary, pub, out = self.run_stop("lost-wait", "throw", csv=None, streams=(b"", LOST_LINE.encode("ascii")))
        self.assertEqual(code, 23, out)
        capture = json.loads((pub / "presentmon-capture.json").read_text(encoding="utf-8"))
        self.assertTrue(capture["eventsLost"]["detected"])
        self.assertEqual(capture["eventsLost"]["maxReported"], 15318)
        self.assertIn("PRESENTMON_EVENTS_LOST", summary["reason"])
        self.assertIn("PRESENTMON_TIMEOUT", summary["reason"], "the original reason is kept, the detail is added")
        self.assertIn("PRESENTMON_EVENTS_LOST", out, "and so is the RESULT line")

    def test_a_clean_wait_failure_carries_no_detail(self) -> None:
        code, summary, pub, out = self.run_stop("clean-wait", "throw", csv=None, streams=(b"", b"Started recording.\n"))
        self.assertEqual(code, 23, out)
        self.assertFalse(json.loads((pub / "presentmon-capture.json").read_text(encoding="utf-8"))["eventsLost"]["detected"])
        self.assertNotIn("PRESENTMON_EVENTS_LOST", summary["reason"])

    def test_the_display_report_failure_reads_as_the_cause_not_as_output_does_not_exist(self) -> None:
        code, summary, _pub, out = self.run_parse_with_events("lost-parse", events_lost_stderr=LOST_LINE.encode("ascii"))
        self.assertEqual(code, 23, out)
        self.assertTrue(summary["reason"].startswith(UM2_REASON), summary["reason"])
        self.assertIn("PRESENTMON_EVENTS_LOST", summary["reason"])
        self.assertIn("PRESENTMON_EVENTS_LOST", out)

    def test_a_display_report_failure_without_a_loss_keeps_its_reason_verbatim(self) -> None:
        code, summary, _pub, out = self.run_parse_with_events("clean-parse")
        self.assertEqual(code, 23, out)
        self.assertEqual(summary["reason"], UM2_REASON)

    SWEEP_CALL = "$displayReport = Add-AttrCudaPresentMonOrphanSweepDetailToReport -Report $displayReport -Sweep $presentMonOrphanSweep"
    SKIPPED_SWEEP = "$presentMonOrphanSweep = [ordered]@{ ran = $false; skippedReason = 'presentmon_process_alive'; livePresentMonProcessIds = @(43684) }\n"

    def test_a_display_report_failure_after_a_skipped_sweep_names_the_sweep(self) -> None:
        # BACHELOR-CUDA-PRESENTMON-CSV-MISSING-1: the 16 Bachelor cuda legs of 2026-10-27 read only "PresentMon output does not exist" while the sweep had been skipped on every one
        injected = mutate(TEMPLATE, self.SWEEP_CALL, self.SKIPPED_SWEEP + self.SWEEP_CALL)
        code, summary, _pub, out = self.run_parse_with_events("skipped-sweep-parse", text=injected)
        self.assertEqual(code, 23, out)
        self.assertTrue(summary["reason"].startswith(UM2_REASON), summary["reason"])
        self.assertIn("[PRESENTMON_ORPHAN_SWEEP_INCOMPLETE: skipped=presentmon_process_alive livePids=43684]", summary["reason"])
        self.assertIn("PRESENTMON_ORPHAN_SWEEP_INCOMPLETE", out, "and so is the RESULT line")

    def test_mutation_without_the_sweep_detail_the_reason_is_bare_again(self) -> None:
        injected = mutate(TEMPLATE, self.SWEEP_CALL, self.SKIPPED_SWEEP + "$null = $presentMonOrphanSweep")
        _code, summary, _pub, _out = self.run_parse_with_events("mut-sweep-detail", text=injected)
        self.assertEqual(summary["reason"], UM2_REASON)

    def test_both_capture_json_statements_carry_the_orphan_sweep_and_the_events_lost_record(self) -> None:
        self.assertEqual(TEMPLATE.count("orphanSweep=$presentMonOrphanSweep"), 2)
        self.assertEqual(TEMPLATE.count("eventsLost=$presentMonEventsLost"), 2)

    def test_the_events_lost_scan_runs_after_the_stop_and_before_the_terminals(self) -> None:
        block = wait_block()
        self.assertLess(block.index("Publish-AttrCudaPresentMonCaptureEvidence"), block.index("$presentMonEventsLost = Get-AttrCudaPresentMonEventsLost"))
        self.assertLess(block.index("$presentMonEventsLost = Get-AttrCudaPresentMonEventsLost"), block.rindex("exit 23"))

    def test_mutation_without_the_wait_failure_detail_the_reason_is_bare_again(self) -> None:
        mutated = mutate(TEMPLATE, "$presentMonWaitError = Add-AttrCudaPresentMonEventsLostDetail -Reason $presentMonWaitError -EventsLost $presentMonEventsLost", "$null = $presentMonEventsLost")
        _code, summary, _pub, _out = self.run_stop("mut-wait-detail", "throw", csv=None, streams=(b"", LOST_LINE.encode("ascii")), text=mutated)
        self.assertNotIn("PRESENTMON_EVENTS_LOST", summary["reason"])

    def test_mutation_without_the_display_report_detail_the_reason_is_bare_again(self) -> None:
        mutated = mutate(TEMPLATE, "$displayReport = Add-AttrCudaPresentMonEventsLostDetailToReport -Report $displayReport -EventsLost $presentMonEventsLost", "$null = $presentMonEventsLost")
        _code, summary, _pub, _out = self.run_parse_with_events("mut-parse-detail", events_lost_stderr=LOST_LINE.encode("ascii"), text=mutated)
        self.assertNotIn("PRESENTMON_EVENTS_LOST", summary["reason"])

    def test_mutation_without_the_scan_the_capture_json_has_no_record(self) -> None:
        mutated = mutate(TEMPLATE, "$presentMonEventsLost = Get-AttrCudaPresentMonEventsLost -StderrPath $presentMonStderrPath", "$presentMonEventsLost = $null")
        _code, _summary, pub, _out = self.run_stop("mut-scan", "throw", csv=None, streams=(b"", LOST_LINE.encode("ascii")), text=mutated)
        self.assertIsNone(json.loads((pub / "presentmon-capture.json").read_text(encoding="utf-8"))["eventsLost"])


# --------------------------------------------------------------------------------------------------------------------- static pins
class StaticTests(unittest.TestCase):
    def test_every_new_module_function_is_exported_and_spliced_into_the_job_inside_its_bracket(self) -> None:
        generator = lf(GENERATOR.read_text(encoding="utf-8"))
        module = MODULE.read_text(encoding="utf-8")
        for name in NEW_MODULE_FUNCTIONS:
            self.assertIn(f"function {name} {{", module)
            self.assertIn(f"'{name}'", generator, "named in the generator's embed list")
        export = module[module.index("Export-ModuleMember -Function"):]
        for name in NEW_MODULE_FUNCTIONS:
            self.assertIn(name, export)

    def test_the_template_text_added_by_the_card_is_bracketed(self) -> None:
        opens = TEMPLATE.count(OPEN)
        closes = TEMPLATE.count(CLOSE)
        self.assertEqual(opens, closes)
        self.assertGreater(opens, 0)

    def test_the_sweep_function_never_uses_add_type_or_a_static_the_write_scan_does_not_list(self) -> None:
        sweep = _slice(TEMPLATE, "function Get-PresentMonLiveProcessId(", "\nfunction Split-PresentMonCsvLine(")
        self.assertNotIn("Add-Type", sweep)
        self.assertNotIn("::", sweep)

    def test_the_spawn_failure_summary_carries_the_sweep_record(self) -> None:
        block = _slice(TEMPLATE, "if ($null -ne $presentMonSpawnError) {", "$presentMonPostSpawnUtc = (Get-Date).ToUniversalTime()")
        self.assertIn("presentMonOrphanSweep=$presentMonOrphanSweep", block)


if __name__ == "__main__":
    unittest.main()
